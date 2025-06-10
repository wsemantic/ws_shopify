# inherirt class sale.order and add fields for shopify instance and shopify order id
import datetime
import json
import requests
from dateutil import parser
from pytz import utc
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import logging
import re

_logger = logging.getLogger(__name__)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    shopify_order_map_ids = fields.One2many(
        'shopify.order.map',  # modelo relacionado
        'order_id',           # campo inverso en shopify.order.map
        string='Shopify Order Maps'
    )

    def import_shopify_draft_orders(self, shopify_instance_ids, skip_existing_order, from_date, to_date):
        # Importa órdenes en borrador desde Shopify a Odoo
        if shopify_instance_ids == False:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active', '=', True)])
        for shopify_instance_id in shopify_instance_ids:
            url = self.get_order_url(shopify_instance_id, endpoint='draft_orders.json')
            access_token = shopify_instance_id.shopify_shared_secret
            headers = {
                "X-Shopify-Access-Token": access_token
            }
            effective_from_date = from_date or shopify_instance_id.shopify_last_date_order_import          
                
            # Configurar parámetros para la consulta a Shopify
            params = {
                "limit": 250,  # Ajusta el tamaño de página según sea necesario
                "pageInfo": None,
                "status": "any"
            }
            if effective_from_date:
                params["created_at_min"] = effective_from_date
            if to_date:
                params["created_at_max"] = to_date  
            all_orders = []

            while True:
                response = requests.get(url, headers=headers, params=params)
                if response.status_code == 200 and response.content:
                    draft_orders = response.json()
                    orders = draft_orders.get('draft_orders', [])
                    all_orders.extend(orders)
                    page_info = draft_orders.get('page_info', {})
                    if 'has_next_page' in page_info and page_info['has_next_page']:
                        params['page_info'] = page_info['next_page']
                    else:
                        break
                else:
                    _logger.info("Error: %s", response.status_code)
                    break
            if all_orders:
                orders = self.create_shopify_order(all_orders, shopify_instance_id, skip_existing_order, status='draft')
                shopify_instance_id.shopify_last_date_order_import = fields.Datetime.now()
                return orders
            else:
                _logger.info("No draft orders found in Shopify.")
                return []

    def create_shopify_order(self, orders, shopify_instance_id, skip_existing_order, status):
        # Crea o actualiza órdenes en Odoo a partir de datos de Shopify
        order_list = []
        for order in orders:
            _logger.info(f"WSSH iterando orden {order.get('name')}")
            shopify_order_map = self.env['shopify.order.map'].sudo().search([
                ('shopify_order_id', '=', order.get('id')),
                ('shopify_instance_id', '=', shopify_instance_id.id)
            ], limit=1)
            if not shopify_order_map:
                _logger.info(f"WSSH no existe map para {order.get('name')}, creando la orden.")
                sale_order_rec = self.prepare_shopify_order_vals(shopify_instance_id, order, skip_existing_order)
            else:
                _logger.info(f"WSSH encontrado map para {order.get('name')}, usando la orden existente.")
                sale_order_rec = shopify_order_map.order_id
            if sale_order_rec:
                # Sincronizar el nombre desde Shopify, actualizándolo en Odoo
                sale_order_rec.name = order.get('name')
                order_list.append(sale_order_rec.id)

        return order_list   


    def prepare_shopify_order_vals(self, shopify_instance_id, order, skip_existing_order):
        # Paso 1: Detectar si el pedido contiene un producto de "Recargo de Equivalencia"
        apply_recargo_fiscal_position = False
        for line in order.get('line_items', []):
            product_name = line.get('title', '')
            if re.search(r'recargo.*equivalencia', product_name, re.IGNORECASE):
                apply_recargo_fiscal_position = True
                break  # No necesitamos seguir buscando

        # Procesar el cliente y preparar los valores del pedido
        if order.get('customer'):
            res_partner = self.env['res.partner'].get_or_create_partner_from_shopify(order.get('customer'), shopify_instance_id)
            if res_partner:
                dt = parser.isoparse(order.get('created_at'))
                                                  
                dt_utc = dt.astimezone(utc)  # Convertir a UTC
                date_order_value = fields.Datetime.to_string(dt_utc)

                # Paso 2: Asignar la posición fiscal al cliente si aplica recargo
                if apply_recargo_fiscal_position:
                    fiscal_position = self.env.ref('l10n_es.1_fp_recargo')
                    if fiscal_position:
                        res_partner.property_account_position_id = fiscal_position.id
                        _logger.info(f"WSSH Asignada posición fiscal de recargo al cliente {res_partner.name}")
                    else:
                        raise UserError(_(f"WSSH Posición fiscal de recargo no encontrada"))

                # Procesar dirección de envío
                partner_shipping_id = res_partner.id  # Por defecto, usar el mismo partner
                shipping_address = order.get('shipping_address')
                if shipping_address:
                    # Verificar si la dirección de envío es diferente a la de facturación
                    billing_address = order.get('billing_address') or order.get('customer', {}).get('default_address', {})
                    if res_partner.addresses_are_different(shipping_address, billing_address):
                        # Crear o buscar partner hijo para dirección de envío
                        partner_shipping_id = res_partner.get_or_create_shipping_partner(
                            shipping_address, res_partner, shopify_instance_id
                        )
                        _logger.info(f"WSSH Dirección de envío diferente procesada para pedido {order.get('name')}")

                # Preparar valores para el pedido
                sale_order_vals = {
                    'partner_id': res_partner.id,
                    'partner_shipping_id': partner_shipping_id,
                    'name': order.get('name'),
                    'create_date': date_order_value,
                    'date_order': date_order_value,
                    'user_id': shopify_instance_id.salesperson_id.id if shopify_instance_id.salesperson_id else False,
                }

                # Crear el pedido
                sale_order_rec = self.sudo().create(sale_order_vals)
                                             
                sale_order_rec.sudo().write({'date_order': date_order_value})
                  
                                                                      
                sale_order_rec.state = 'draft'

                # Paso 3: Asignar la posición fiscal al pedido si aplica recargo
                if apply_recargo_fiscal_position and fiscal_position:
                    sale_order_rec.fiscal_position_id = fiscal_position.id
                    _logger.info(f"WSSH Asignada posición fiscal de recargo al pedido {sale_order_rec.name}")

                # Crear el mapeo con Shopify
                map_vals = {
                    'order_id': sale_order_rec.id,
                    'shopify_order_id': order.get('id'),
                    'shopify_instance_id': shopify_instance_id.id,
                }
                shopify_map = self.env['shopify.order.map'].sudo().create(map_vals)
                sale_order_rec.write({'shopify_order_map_ids': [(4, shopify_map.id)]})

                # Procesar las líneas del pedido
                self.create_shopify_order_line(sale_order_rec, order, skip_existing_order, shopify_instance_id)

                return sale_order_rec
                
    def create_shopify_order_line(self, shopify_order_id, order, skip_existing_order, shopify_instance_id):
        # Crea líneas de orden de venta en Odoo basadas en las líneas de Shopify
        # Calcular el porcentaje total de descuento usando total_discounts antes de procesar las líneas
        total_discount_percentage = 0.0
        total_discounts = float(order.get('total_discounts', 0.0))
        if total_discounts > 0:
            subtotal = sum(float(line.get('price', 0)) * line.get('quantity', 1) for line in order.get('line_items', []))
            if subtotal > 0:
                total_discount_percentage = round((total_discounts / subtotal) * 100, 2)

        # Si hay líneas existentes y no se salta la actualización, eliminarlas
        if shopify_order_id.order_line and not skip_existing_order:
            shopify_order_id.order_line.unlink()

        dict_tax = {}
        for line in order.get('line_items'):
            product_name = line.get('title', '')
            if re.search(r'recargo.*equivalencia', product_name, re.IGNORECASE):
                continue
            tax_list = []
            if line.get('tax_lines'):
                for tax_line in line.get('tax_lines'):
                    dict_tax['name'] = tax_line.get('title')
                    if tax_line.get('rate'):
                        dict_tax['amount'] = tax_line.get('rate') * 100
                    tax = self.env['account.tax'].sudo().search([('name', '=', tax_line.get('title'))], limit=1)
                    if tax:
                        tax.sudo().write(dict_tax)
                    else:
                        tax = self.env['account.tax'].sudo().create(dict_tax)
                    if tax_line.get('price') != '0.00':
                        tax_list.append(tax.id)
            product = self.env['product.product'].sudo().search([
                ('shopify_variant_map_ids.web_variant_id', '=', line.get('variant_id')),
                ('shopify_variant_map_ids.shopify_instance_id', '=', shopify_instance_id.id)
            ], limit=1)
            if not product:
                # Intentar buscar por SKU (default_code) antes de usar el genérico
                sku = line.get('sku') or ''
                if sku:
                    product_by_sku = self.env['product.product'].sudo().search([
                        '|',
                        ('default_code', '=', sku),
                        ('barcode', '=', sku),
                    ], limit=1)
                    if product_by_sku:
                        # Verificar si existe un mapeo para este producto en la instancia actual
                        product_map = product_by_sku.shopify_variant_map_ids.filtered(
                            lambda m: m.shopify_instance_id == shopify_instance_id 
                        )
                        if product_map:
                            product = product_by_sku
                            product_name = line.get('title')+' '+sku
                        else:
                            # Si no hay mapeo, seguir con el genérico pero loguear la situación
                            _logger.info(f"WSSH Producto encontrado por SKU {sku} pero sin mapeo para variant_id {line.get('variant_id')} en instancia {shopify_instance_id.name}")
                if not product:
                    # Buscar/crear por nombre si no se encontró por SKU
                    product_by_name = self.env['product.product'].sudo().search([
                        ('name', '=', line.get('title'))
                    ], limit=1)
                    
                    if product_by_name:
                        product = product_by_name
                        product_name = line.get('title')
                    else:
                        # Si no existe, crear el producto
                        product = self._create_or_get_product(line.get('title'), sku, 'product')
                        product_name = line.get('title')
            else:
                product_name = line.get('title')
                
            if product:
                # Precio recibido de Shopify (incluye IVA y descuento ya aplicado)
                price_incl = float(line.get('price')) # aplico descuento al precio globalmente con campo total_discount_percentage más abajo #- float(line.get('total_discount', 0))
                tax_rate_total = 0.0
                for tax_line in line.get('tax_lines', []):
                    if tax_line.get('rate'):
                        tax_rate_total += float(tax_line.get('rate'))
                # Si los precios no incluyen IVA, no dividir; usar el precio tal cual
                if shopify_instance_id.prices_include_tax:
                    price_excl = round(price_incl / (1 + tax_rate_total), 2) if tax_rate_total else price_incl
                else:
                    price_excl = price_incl

                shopify_order_line_vals = {
                    'order_id': shopify_order_id.id,
                    'product_id': product.id,
                    'name': product_name,
                    'product_uom_qty': line.get('quantity'),
                    'price_unit': price_excl,
                    'discount': total_discount_percentage if total_discount_percentage else 0.0,
                    'tax_id': [(6, 0, tax_list)]
                }
                self.env['sale.order.line'].sudo().create(shopify_order_line_vals)
        
        for lineship in order.get('shipping_lines'):
            price = round(float(lineship.get('price')) / 1.21, 2)
            if price > 0:
                delivery_product = self.env['product.product'].sudo().search(
                    [('name', '=', lineship.get('title'))], limit=1)
                if not delivery_product:
                    delivery_product = self._create_or_get_product(lineship.get('title'), '', 'shipping')
                
                if delivery_product:
                    shipping_vals = {
                        'product_id': delivery_product.id,
                        'name': "Shipping",
                        'price_unit': float(lineship.get('price')),
                        'order_id': shopify_order_id.id,
                        'tax_id': [(6, 0, [])]
                    }
                    self.env['sale.order.line'].sudo().create(shipping_vals)

        return True
        
    def _create_or_get_product(self, name, sku='', product_type='product'):
        """
        Centraliza la creación de productos tanto para líneas de pedido como para gastos de envío
        
        Args:
            name: Nombre del producto
            sku: Código SKU (opcional)
            product_type: Tipo de producto ('product' o 'shipping')
            
        Returns:
            product.product: El producto creado o encontrado
        """
        product_vals = {
            'name': name,
            'detailed_type': 'product',
        }
        
        if sku:
            product_vals['default_code'] = sku
            
        product = self.env['product.product'].sudo().create(product_vals)
        
        # Si es un producto de envío, crear también el carrier
        if product_type == 'shipping':
            shopify_instance_id = self.env.context.get('shopify_instance_id')
            if shopify_instance_id:
                vals = {
                    'is_shopify': True,
                    'shopify_web_id': shopify_instance_id.id,
                    'name': shopify_instance_id.name + '.' + name,
                    'product_id': product.id,
                }
                shipping = self.env['delivery.carrier'].sudo().create(vals)
                
        return product

    def get_order_url(self, shopify_instance_id, endpoint):
        # Construye la URL para la API de Shopify basada en la instancia y el endpoint
        shop_url = "https://{}.myshopify.com/admin/api/{}/{}".format(shopify_instance_id.shopify_host,
                                                                     shopify_instance_id.shopify_version, endpoint)
        return shop_url

    def check_customer(self, customer, shopify_instance_id):
        # Este método ya no es necesario, se elimina y se usa get_or_create_partner_from_shopify directamente
        pass

    def import_shopify_orders(self, shopify_instance_ids, skip_existing_order, from_date, to_date):
        # Importa órdenes completas desde Shopify a Odoo

        if not shopify_instance_ids:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active', '=', True)])
            _logger.info(f"WSSH Importa ordenes todas las instancias encontradas {len(shopify_instance_ids)}")
        else:
            _logger.info(f"WSSH Importa ordenes para ids {shopify_instance_ids}")
            
        orders_total = []
        
        for shopify_instance_id in shopify_instance_ids:
            self.import_shopify_draft_orders(shopify_instance_id, skip_existing_order, from_date, to_date)
            # import shopify oders from shopify to odoo
            # call method to connect to shopify

            all_orders = []
            url = self.get_order_url(shopify_instance_id, endpoint='orders.json')
            access_token = shopify_instance_id.shopify_shared_secret
            headers = {
                "X-Shopify-Access-Token": access_token
            }
            
            effective_from_date = from_date or shopify_instance_id.shopify_last_date_order_import          
                
            # Configurar parámetros para la consulta a Shopify
            params = {
                "limit": 250,  # Ajusta el tamaño de página según sea necesario
                "pageInfo": None,
                "status": "any"
            }
            if effective_from_date:
                params["created_at_min"] = effective_from_date
            if to_date:
                params["created_at_max"] = to_date  
                
            while True:
                response = requests.get(url, headers=headers, params=params)
                if response.status_code == 200 and response.content:
                    data = response.json()
                    orders = data.get('orders', [])
                    all_orders.extend(orders)

                    page_info = data.get('page_info', {})
                    if 'has_next_page' in page_info and page_info['has_next_page']:
                        params['page_info'] = page_info['next_page']
                    else:
                        break
                else:
                    _logger.info("Error: %s", response.status_code)
                    break
            if all_orders:
                _logger.info(f"WSSH Found {len(all_orders)} para {shopify_instance_id.name}")
                orders = self.create_shopify_order(all_orders, shopify_instance_id, skip_existing_order, status='open')
                orders_total.extend(orders)
                shopify_instance_id.shopify_last_date_order_import = fields.Datetime.now()
            else:
                _logger.info(f"WSSH No orders found in shopify {shopify_instance_id.name}")
                
        return orders_total
     

    def export_orders_to_shopify(self, shopify_instance_ids, update):
        # Exporta órdenes de Odoo a Shopify como borradores
        order_ids = self.sudo().browse(self._context.get("active_ids"))
        if not order_ids:
            if update == False:
                order_ids = self.sudo().search([('shopify_order_map_ids', '=', False)])
            else:
                order_ids = self.sudo().search([])

        if shopify_instance_ids == False:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active', '=', True)])
        for instance_id in shopify_instance_ids:
            url = self.get_order_url(instance_id, endpoint='draft_orders.json')
            access_token = instance_id.shopify_shared_secret

            headers = {
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json"
            }

            response = ""
            for order in order_ids:
                line_val_list = []
                discount_val = []
                if order.order_line:
                    for line in order.order_line:
                        line_vals_dict = {
                            'title': line.product_id.name,
                            'price': line.price_unit,
                            'quantity': int(line.product_uom_qty),
                            "tax_lines": [],
                            "applied_discount": {
                                "description": "Custom discount",
                                "value_type": "percentage",
                                "value": line.discount,
                                "amount": line.price_unit - line.price_subtotal,
                                "title": "Custom"
                            }
                        }
                        line_val_list.append(line_vals_dict)
                        discount_val.append(line.discount)
                        
                map_for_instance = order.shopify_order_map_ids.filtered(
                    lambda m: m.shopify_instance_id.id == instance_id.id
                )
                map_for_partner = order.partner_id.shopify_partner_map_ids.filtered(
                    lambda m: m.shopify_instance_id.id == instance_id.id
                )
                customer_id = map_for_partner.shopify_partner_id if map_for_partner else None

                if map_for_instance and update == True:
                    shopify_map = map_for_instance[0]
                    end = "draft_orders/{}.json".format(shopify_map.shopify_order_id)
                    url_update = self.get_order_url(instance_id, endpoint=end)
                    payload = {
                        "draft_order": {
                            "id": shopify_map.shopify_order_id,
                            "line_items": line_val_list,
                            "customer": {
                                "id": customer_id
                            } if customer_id else {},
                            "tax_lines": [],
                        }
                    }
                    response = requests.put(url_update, headers=headers, data=json.dumps(payload))
                    # Actualizar el registro del mapa si es necesario
                    shopify_map.write({
                        'shopify_order_id': payload.get('draft_order', {}).get('id', shopify_map.shopify_order_id),
                    })
                else:
                    if not map_for_instance:
                        payload = {
                            "draft_order": {
                                "line_items": line_val_list,
                                "customer": {
                                    "id": customer_id
                                } if customer_id else {},
                                "use_customer_default_address": True,
                                "tax_lines": [],
                            }
                        }

                        response = requests.post(url, headers=headers, data=json.dumps(payload))
                        if response and response.content:
                            draft_orders = response.json()
                            draft_order = draft_orders.get('draft_order', [])
                            if draft_order:
                                order.name = draft_order.get('name')
                                # Se crea el registro en la clase mapa con la info de Shopify
                                map_vals = {
                                    'order_id': order.id,
                                    'shopify_order_id': draft_order.get('id'),
                                    'shopify_instance_id': instance_id.id,
                                }
                                shopify_map = self.env['shopify.order.map'].sudo().create(map_vals)
                                order.write({
                                    'shopify_order_map_ids': [(4, shopify_map.id)]
                                })
                                _logger.info("Draft Order Created/Updated Successfully")
                    else:
                        # Caso de actualización sin respuesta o error
                        _logger.info("Draft Order Creation/Updated Failed")
                        _logger.info("Failed", response.content)
                if response:
                    if response.content:
                        _logger.info(response.content)
                else:
                    _logger.info("Nothing Create / Updated")