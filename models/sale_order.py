# inherirt class sale.order and add fields for shopify instance and shopify order id
import datetime
import json
import requests
from dateutil import parser
from pytz import utc
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import logging

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
        # Prepara los valores para crear una orden de venta en Odoo
        # call a method to check the customer is available or not
        # if not available create a customer
        # if available get the customer id
        # create a sale order
        # create a sale order line
        if order.get('customer'):
            res_partner = self.check_customer(order.get('customer'), shopify_instance_id)
            if res_partner:
                dt = parser.isoparse(order.get('created_at'))
                # Convertir a UTC si es necesario:
                dt_utc = dt.astimezone(utc)
                date_order_value = fields.Datetime.to_string(dt_utc)
                
                sale_order_vals = {
                    'partner_id': res_partner.id,
                    'name': order.get('name'),
                    'create_date': date_order_value,
                    'date_order': date_order_value,
                }
                                        
                sale_order_rec = self.sudo().create(sale_order_vals)
                sale_order_rec.sudo().write({
                    'date_order': date_order_value,
                })
                _logger.info("WSSH fecha pedido %s", date_order_value)
                sale_order_rec.state = 'draft'
                # Se crea el registro en la clase mapa con los campos específicos de Shopify
                map_vals = {
                    'order_id': sale_order_rec.id,
                    'shopify_order_id': order.get('id'),
                    'shopify_instance_id': shopify_instance_id.id,
                }
                shopify_map = self.env['shopify.order.map'].sudo().create(map_vals)                
                sale_order_rec.write({
                    'shopify_order_map_ids': [(4, shopify_map.id)]
                })
                self.create_shopify_order_line(sale_order_rec, order, skip_existing_order, shopify_instance_id)

                return sale_order_rec

    def create_shopify_order_line(self, shopify_order_id, order, skip_existing_order, shopify_instance_id):
        # Crea líneas de orden de venta en Odoo basadas en las líneas de Shopify
        amount = 0.00
        discount = 0.00
        if order.get('applied_discount'):
            amount = float(order.get('applied_discount').get('amount'))

        if len(order.get('line_items')) > 1:
            discount = amount / len(order.get('line_items'))
        else:
            discount = amount

        dict_tax = {}

        if shopify_order_id.order_line and skip_existing_order == False:
            shopify_order_id.order_line = [(5, 0, 0)]
            
        for line in order.get('line_items'):
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
                generic_product = self.env.ref('ws_shopify_split_color.product_generic', raise_if_not_found=False)
                if not generic_product:
                    raise UserError(_(f"No se ha definido el producto {line.get('title')} {line.get('product_id')} variante {line.get('variant_id')}."))
                product = generic_product
                product_name = "{} - {}".format(generic_product.name, line.get('title'))

            else:
                product_name = line.get('title')
                
            if product:
                # Precio recibido de Shopify (incluye IVA)
                price_incl = float(line.get('price')) - float(line.get('total_discount'))

                # Calcular la tasa total de IVA a partir de tax_lines, o definir una tasa fija
                tax_rate_total = 0.0
                for tax_line in line.get('tax_lines', []):
                    if tax_line.get('rate'):
                        tax_rate_total += float(tax_line.get('rate'))
                # En caso de que no exista información de impuestos, se puede asumir 0%
                if tax_rate_total:
                    price_excl = round(price_incl / (1 + tax_rate_total), 2)
                else:
                    price_excl = price_incl

                subtotal = price_excl * line.get('quantity')

                shopify_order_line_vals = {
                    'order_id': shopify_order_id.id,
                    'product_id': product.id,
                    'name': product_name,
                    'product_uom_qty': line.get('quantity'),
                    'price_unit': price_excl,
                    'discount': (discount / subtotal) * 100 if discount else 0.00,
                    'tax_id': [(6, 0, tax_list)]
                }
                shopify_order_line_id = self.env['sale.order.line'].sudo().create(shopify_order_line_vals)
        
        for lineship in order.get('shipping_lines'):
            price = round(float(lineship.get('price')) / 1.21, 2)
            if price > 0:
                shipping = self.env['delivery.carrier'].sudo().search(
                    [('name', '=', lineship.get('title')), ('shopify_web_id', '=', shopify_instance_id.id)], limit=1)
                if not shipping:
                    delivery_product = self.env['product.product'].sudo().create({
                        'name': shopify_instance_id.name + '.' + lineship.get('title'),
                        'detailed_type': 'product',
                    })
                    vals = {
                        'is_shopify': True,
                        'shopify_web_id': shopify_instance_id.id,
                        'name': lineship.get('title'),
                        'product_id': delivery_product.id,
                    }
                    shipping = self.env['delivery.carrier'].sudo().create(vals)
                if shipping and shipping.product_id:
                    shipping_vals = {
                        'product_id': shipping.product_id.id,
                        'name': "Shipping",
                        'price_unit': float(lineship.get('price')),
                        'order_id': shopify_order_id.id,
                        'tax_id': [(6, 0, [])]
                    }
                    shipping_so_line = self.env['sale.order.line'].sudo().create(shipping_vals)

        return True

    def get_order_url(self, shopify_instance_id, endpoint):
        # Construye la URL para la API de Shopify basada en la instancia y el endpoint
        shop_url = "https://{}.myshopify.com/admin/api/{}/{}".format(shopify_instance_id.shopify_host,
                                                                     shopify_instance_id.shopify_version, endpoint)
        return shop_url

    def check_customer(self, customer, shopify_instance_id):
        # check customer is available or not
        # if not available create a customer and pass it
        # if available write and pass the customer
        default_address = customer.get('default_address', {})

        # Priorizar campos directos del cliente, con fallback a default_address
        first_name = customer.get('first_name') or default_address.get('first_name') or ''
        last_name = customer.get('last_name') or default_address.get('last_name') or ''
        email = customer.get('email') or default_address.get('email') or ''
        phone = customer.get('phone') or default_address.get('phone') or ''
        
        # Construir el nombre del cliente
        name = (first_name + ' ' + last_name).strip() or email or _("Shopify Customer")
        _logger.info(f"WSSH nombre construido para cliente: {name}")

        # Buscar primero por shopify_partner_id para asegurar que se use si existe
        partner_obj = self.env['res.partner'].sudo().search([
            ('shopify_partner_map_ids.shopify_partner_id', '=', customer.get('id')),
            ('shopify_partner_map_ids.shopify_instance_id', '=', shopify_instance_id.id)
        ], limit=1)
        
        # Si no se encuentra por ID, buscar por email
        if not partner_obj:
            partner_obj = self.env['res.partner'].sudo().search([
                ('email', '=', email)
            ], limit=1)

        # Preparar valores del cliente
        customer_vals = {
            'name': name,
            'email': email,
            'phone': phone,
            'is_shopify_customer': True,
        }
        
        # Añadir tags si existen
        tags = customer.get('tags')
        tag_list = []
        if tags:
            tags = tags.split(',')
            for tag in tags:
                tag_id = self.env['res.partner.category'].sudo().search([('name', '=', tag)], limit=1)
                if not tag_id:
                    tag_id = self.env['res.partner.category'].sudo().create({'name': tag})
                tag_list.append(tag_id.id)
            customer_vals['category_id'] = [(6, 0, tag_list)]

        # Si no se encontró un partner, crearlo
        if not partner_obj:
            _logger.info(f"WSSH Creando nuevo cliente: {name}")
            partner_obj = self.env['res.partner'].sudo().create(customer_vals)
            self.env['shopify.partner.map'].sudo().create({
                'partner_id': partner_obj.id,
                'shopify_partner_id': customer.get('id'),
                'shopify_instance_id': shopify_instance_id.id,
            })
        else:
            _logger.info(f"WSSH Cliente existente encontrado: {partner_obj.name}")
            # No actualizamos el partner aquí, solo aseguramos el mapping
            mapping = partner_obj.shopify_partner_map_ids.filtered(
                lambda m: m.shopify_instance_id == shopify_instance_id
            )
            if not mapping:
                self.env['shopify.partner.map'].sudo().create({
                    'partner_id': partner_obj.id,
                    'shopify_partner_id': customer.get('id'),
                    'shopify_instance_id': shopify_instance_id.id,
                })

        return partner_obj

    def import_shopify_orders(self, shopify_instance_ids, skip_existing_order, from_date, to_date):
        # Importa órdenes completas desde Shopify a Odoo
        if shopify_instance_ids == False:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active', '=', True)])
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
                "page_info": None,
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
                orders = self.create_shopify_order(all_orders, shopify_instance_id, skip_existing_order, status='open')
                shopify_instance_id.shopify_last_date_order_import = fields.Datetime.now()
                return orders
            else:
                _logger.info("No orders found in shopify")
                return []        

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