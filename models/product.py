# -*- coding: utf-8 -*-
import base64
import datetime
import time
import json

import requests, re
from bs4 import BeautifulSoup
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import config
config['limit_time_real'] = 10000000
import logging

_logger = logging.getLogger(__name__)

# Mapeo de tallas con letras a valores numéricos para ordenación
SIZE_MAPPING = {
    '4XS': 0,
    'XXXXS': 0,
    'XXXS': 1,
    '3XS': 1,
    'XXS': 2,
    '2XS': 2,
    'XS': 3,
    'S': 4,
    'M': 5,
    'L': 6,
    'XL': 7,
    'XXL': 8,
    '2XL': 8,
    'XXXL': 9,
    '3XL': 9,
    'XXXXL': 10,
    '4XL': 10,
}

def get_size_value(size):
    """Convierte una talla en un valor comparable para ordenación."""
    if not size:
        return (0, 0)  # Prioridad numérica, valor 0
    
    size = size.upper().strip()
    if size in SIZE_MAPPING:
        return (0, SIZE_MAPPING[size])  # Prioridad numérica, valor del mapeo
    
    # Manejar tallas como "2XL" o "3XS"
    match = re.match(r'(\d+)X([SLM])', size)
    if match:
        _logger.info(f"WSSH get_size_value Fallback Match {size}")
        num_x = int(match.group(1))
        base_size = match.group(2)
        if base_size == 'S':
            return (0, SIZE_MAPPING['S'] - num_x)
        elif base_size == 'L':
            return (0, SIZE_MAPPING['L'] + num_x)
        elif base_size == 'M':
            return (0, SIZE_MAPPING['M'])
    
    # Intentar convertir a número para tallas numéricas
    try:
        return (0, float(size))  # Prioridad numérica, valor numérico
    except ValueError:
        _logger.info(f"WSSH get_size_value Excepcion {size}")
        return (1, size.lower())  # Prioridad alfabética, cadena en minúsculas

class ProductTemplateAttributeValue(models.Model):
    _inherit = 'product.template.attribute.value'

    shopify_product_map_ids = fields.One2many(
        "shopify.product.map",
        "odoo_id",
        string="Shopify Product Mappings",
        help="Mappings to Shopify products across multiple websites"
    )

# inherit class product.product and add fields for shopify instance and shopify variant id

class ProductProduct(models.Model):
    _inherit = 'product.product'

    is_shopify_variant = fields.Boolean('Is Shopify Variant', default=False)
    shopify_barcode = fields.Char('Shopify Barcode')
    shopify_sku = fields.Char('Shopify SKU')
    # Propiedad calculada para el mapeo de stock
    shopify_stock_map_ids = fields.One2many(
        'shopify.stock.map', 
        'odoo_id',   
        string="Shopify Stock Mappings Computed",
        help='Mapeos entre el item inventory de Shopify y la variante en Odoo'
    )

    shopify_variant_map_ids = fields.One2many(
        "shopify.variant.map",
        "odoo_id",
        string="Shopify Variant Mappings",
        help="Mappings to Shopify variants across multiple websites"
    )


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    wholesale_price = fields.Float(
        string='Precio Mayorista',
        help='Precio especial para ventas al por mayor.',
        digits='Product Price'
    )

    shopify_product_map_ids = fields.One2many(
        "shopify.product.template.map",
        "odoo_id",
        string="Shopify Product Mappings",
        help="Mappings to Shopify products across multiple websites"
    )
    
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('detailed_type') == 'product':
                vals['is_published'] = True
        return super(ProductTemplate, self).create(vals_list)
        
    def get_products_url(self, shopify_instance_id, endpoint):
        shop_url = "https://{}.myshopify.com/admin/api/{}/{}".format(shopify_instance_id.shopify_host,
                                                                     shopify_instance_id.shopify_version, endpoint)
        return shop_url

    def import_shopify_products_sub(self, shopify_instance_id, skip_existing_products, from_date, to_date, sort_order="asc"):
        """Fetch products from a single Shopify instance."""
        _logger.info("WSSH Starting product import for instance %s", shopify_instance_id.name)

        url = self.get_products_url(shopify_instance_id, endpoint='products.json')
        access_token = shopify_instance_id.shopify_shared_secret
        headers = {
            "X-Shopify-Access-Token": access_token,
        }

        params = {
            "limit": 250,
            "order": f"id {sort_order}",
            "pageInfo": None,
        }

        if from_date and to_date:
            params.update({
                "created_at_min": from_date,
                "created_at_max": to_date,
            })

        all_products = []
        while True:
            response = requests.get(url, headers=headers, params=params)

            if response.status_code == 200 and response.content:
                shopify_products = response.json()
                products = shopify_products.get('products', [])
                all_products.extend(products)
                _logger.info("WSSH All products fetched : %d", len(all_products))
                link_header = response.headers.get('Link')
                if link_header:
                    links = shopify_instance_id._parse_link_header(link_header)
                    if 'next' in links:
                        url = links['next']
                        params = None
                        continue
            break

        _logger.info("WSSH Total products fetched from Shopify: %d", len(all_products))

        if all_products:
            products = self._process_imported_products(all_products, shopify_instance_id, skip_existing_products)
            return products
        else:
            _logger.info("WSSH No products found in Shopify store for instance %s", shopify_instance_id.name)
            return []

    def import_shopify_products(self, shopify_instance_ids, skip_existing_products, from_date, to_date):
        if not shopify_instance_ids:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active', '=', True)])
        for shopify_instance_id in shopify_instance_ids:
            return self.import_shopify_products_sub(
                shopify_instance_id,
                skip_existing_products,
                from_date,
                to_date,
                sort_order="asc",
            )

    def _process_imported_products(self, shopify_products, shopify_instance_id, skip_existing_products):
        product_list = []
        for shopify_product in shopify_products:
            _logger.info("WSSH Processing Shopify product ID: %s", shopify_product.get('id'))
            shopify_product_id = shopify_product.get('id')
            
            # Cambio: Verificar si la instancia usa split por color
            split_by_color = shopify_instance_id.split_products_by_color
            
            if split_by_color:
                # Buscar si el producto ya existe en Odoo por shopify_product_id en product.template.attribute.value          
                existing_attribute_value = self.env['product.template.attribute.value'].sudo().search([
                    ('shopify_product_map_ids.web_product_id', '=', shopify_product_id),
                    ('shopify_product_map_ids.shopify_instance_id', '=', shopify_instance_id.id),
                ], limit=1)
                
                if existing_attribute_value:
                    # Si el producto ya existe, no hacer nada
                    _logger.info(f"WSSH Product with Shopify ID {shopify_product_id} already exists in Odoo for instance {shopify_instance_id.name}.")
                    product_list.append(existing_attribute_value.product_tmpl_id.id)
                    continue
            else:
                # Cambio: Buscar en shopify.product.template.map para modo sin split
                existing_template_map = self.env['shopify.product.template.map'].sudo().search([
                    ('web_product_id', '=', shopify_product_id),
                    ('shopify_instance_id', '=', shopify_instance_id.id),
                ], limit=1)
                if existing_template_map:
                    _logger.info(f"WSSH Product with Shopify ID {shopify_product_id} already exists in Odoo for instance {shopify_instance_id.name}.")
                    product_list.append(existing_template_map.odoo_id.id)
                    continue
            
            # Si no existe, buscar por las variantes (shopify_variant_id o default_code)
            for variant in shopify_product.get('variants', []):
                shopify_variant_id = variant.get('id')

                final_search_domain = [('type', '=', 'product')]

                sku_value = variant.get('sku')
                barcode_value = variant.get('barcode')
                
                if sku_value or barcode_value:
                    final_search_domain.append('|')
                    
                final_search_domain.append(('shopify_variant_map_ids.web_variant_id', '=', shopify_variant_id))

                if sku_value and barcode_value:
                    final_search_domain.append('|')
                
                if sku_value:
                    final_search_domain.append(('default_code', '=', sku_value))
                    
                if barcode_value:
                    final_search_domain.append(('barcode', '=', barcode_value))

                _logger.info(f"WSSH Final search domain for variant ID {shopify_variant_id}: {final_search_domain}")

                # Buscar por las condiciones construidas
                existing_variant = self.env['product.product'].sudo().search(final_search_domain, limit=1)
                
                if existing_variant:
                    if split_by_color:
                        # Filtramos los valores de atributo cuyo atributo sea "color"
                        color_values = existing_variant.product_template_attribute_value_ids.filtered(
                            lambda v: v.attribute_id.name.lower() == 'color'
                        )
                        if color_values:
                            color_map = color_values.shopify_product_map_ids.filtered(
                                lambda m: m.shopify_instance_id == shopify_instance_id)
                            if not color_map:
                                self.env['shopify.product.map'].create({
                                    'web_product_id': shopify_product_id,
                                    'odoo_id': color_values[0].id,
                                    'shopify_instance_id': shopify_instance_id.id,
                                })
                            elif color_map.web_product_id != shopify_product_id:
                                color_map.write({'web_product_id': shopify_product_id})
                            for template_value in color_values:
                                _logger.info(f"WSSH Updated color attribute value {template_value.name} with Shopify ID {shopify_product_id} for instance {shopify_instance_id.name}.")                           
                    else:
                        # Cambio: Crear o actualizar mapa para product.template en modo sin split
                        template_map = self.env['shopify.product.template.map'].sudo().search([
                            ('odoo_id', '=', existing_variant.product_tmpl_id.id),
                            ('shopify_instance_id', '=', shopify_instance_id.id),
                        ], limit=1)
                        if not template_map:
                            self.env['shopify.product.template.map'].create({
                                'web_product_id': shopify_product_id,
                                'odoo_id': existing_variant.product_tmpl_id.id,
                                'shopify_instance_id': shopify_instance_id.id,
                            })
                        elif template_map.web_product_id != shopify_product_id:
                            template_map.write({'web_product_id': shopify_product_id})
                    
                    # Verificar si existe el mapping de variante para el producto
                    variant_map = existing_variant.shopify_variant_map_ids.filtered(
                        lambda m: m.shopify_instance_id == shopify_instance_id)
                    if not variant_map:
                        # Si no existe, aprovechamos el método ya existente para crear tanto
                        # el mapping de variante como el de stock.
                        self._update_variant_ids([existing_variant], shopify_product.get('variants', []), shopify_instance_id)
      
                    _logger.info(f"WSSH Updated existing product template {existing_variant.product_tmpl_id.name} with Shopify ID {shopify_product_id}.")
                    product_list.append(existing_variant.product_tmpl_id.id)
                else:
                    _logger.info("WSSH No matching product found for Shopify Variant ID: %s or SKU: %s", shopify_variant_id, sku_value)
            else:
                # Si no se encuentra el producto ni sus variantes, crear el producto en Odoo
                if not skip_existing_products:
                    _logger.info(f"WSSH Creando producto ")
                    # Cambio: Pasar split_by_color al método de creación
                    #product_template = self._create_product_from_shopify(shopify_product, shopify_instance_id, split_by_color)
                    #if product_template:
                    #    product_list.append(product_template.id)
        
        return product_list

    def _create_product_from_shopify(self, shopify_product, shopify_instance_id, split_by_color=False):  # Cambio: Añadir parámetro split_by_color
        """Crea un producto en Odoo a partir de un producto de Shopify."""
        tags = shopify_product.get('tags')
        tag_list = []
        if tags:
            tags = tags.split(',')
            for tag in tags:
                tag_id = self.env['product.tag'].sudo().search([('name', '=', tag)], limit=1)
                if not tag_id:
                    tag_id = self.env['product.tag'].sudo().create({'name': tag})
                    tag_list.append(tag_id.id)
                else:
                    tag_list.append(tag_id.id)
        
        description = False
        if shopify_product.get('body_html'):
            soup = BeautifulSoup(shopify_product.get('body_html'), 'html.parser')
            description_converted_to_text = soup.get_text()
            description = description_converted_to_text
        
        product_vals = {
            'name': shopify_product.get('title'),
            'is_shopify_product': True,
            "detailed_type": "product",
            'shopify_instance_id': shopify_instance_id.id,
            'default_code': shopify_product.get('sku') if shopify_product.get('sku') else '',
            'barcode': shopify_product.get('barcode') if shopify_product.get('barcode') else '',
            'shopify_barcode': shopify_product.get('barcode') if shopify_product.get('barcode') else '',
            'shopify_sku': shopify_product.get('sku') if shopify_product.get('sku') else '',
            'description_sale': description if description else False,
            'description': shopify_product.get('body_html') if shopify_product.get('body_html') else False,
            'taxes_id': [(6, 0, [])],
            'product_tag_ids': [(6, 0, tag_list)],
        }
        
        # Crear el producto en Odoo
        product_template = self.env['product.template'].sudo().create(product_vals)
        
        # Cambio: Crear mapas según el modo (split o no split)
        if split_by_color:
            # Asignar el shopify_product_id a las líneas de atributos de color
            for attribute_line in product_template.attribute_line_ids:
                for attribute_value in attribute_line.product_template_value_ids:
                    if attribute_value.attribute_id.name.lower() == 'color':
                        self.env['shopify.product.map'].create({
                            'web_product_id': shopify_product.get('id'),
                            'odoo_id': attribute_value.id,
                            'shopify_instance_id': shopify_instance_id.id,
                        })
        else:
            self.env['shopify.product.template.map'].create({
                'web_product_id': shopify_product.get('id'),
                'odoo_id': product_template.id,
                'shopify_instance_id': shopify_instance_id.id,
            })
        
        _logger.info(f"WSSH Created new product template {product_template.name} from Shopify product ID {shopify_product.get('id')}.")
        
        return product_template


    def sync_simple_product_images(self, shopify_image_id, url, product_id,position):
        try:
            response = requests.get(url, stream=True, verify=True, timeout=90)
            if response.status_code == 200:
                image = base64.b64encode(response.content)
                if position == 1:
                    product_id.sudo().write({'image_1920': image})
                shopify_pdt_image = self.env['shopify.product.image'].sudo().search(
                    [('shopify_image_id', '=', shopify_image_id)], limit=1)
                image_vals = {
                    'shopify_image_id': shopify_image_id,
                    'shopify_image': image,
                    'shopify_template_id': product_id.id,
                    'url': url
                }
                if not shopify_pdt_image:
                    shopify_pdt_image = self.env['shopify.product.image'].sudo().create(image_vals)
                else:
                    shopify_pdt_image.sudo().write(image_vals)
        except Exception as error:
            pass

    def sync_variable_product_images(self, shopify_image_id, url, variant_ids):
        try:
            response = requests.get(url, stream=True, verify=True, timeout=90)
            if response.status_code == 200:
                image = base64.b64encode(response.content)
                for variant_id in variant_ids:
                    product_id = self.env['product.product'].sudo().search([('shopify_variant_id', '=', variant_id)])
                    product_id.sudo().write({'image_1920': image})
                    shopify_pdt_image = self.env['shopify.product.image'].sudo().search(
                        [('shopify_image_id', '=', shopify_image_id)], limit=1)
                    image_vals = {
                        'shopify_image_id': shopify_image_id,
                        'shopify_image': image,
                        'shopify_variant_id': product_id.id,
                        'shopify_template_id': product_id.product_tmpl_id.id,
                        'url': url
                    }
                    if not shopify_pdt_image:
                        shopify_pdt_image = self.env['shopify.product.image'].sudo().create(image_vals)
                    else:
                        shopify_pdt_image.sudo().write(image_vals)
        except Exception as error:
            pass

    def update_stock(self, shopify_instance_ids):
        location_ids = self.get_locations()
        if shopify_instance_ids == False:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active', '=', True)])
        for shopify_instance_id in shopify_instance_ids:
            url = self.get_inventory_url(shopify_instance_id, endpoint='inventory_levels.json')
            access_token = shopify_instance_id.shopify_shared_secret
            headers = {
                "X-Shopify-Access-Token": access_token,
            }

            params = {
                "limit": 250,  # Adjust the page size as needed
                "pageInfo": None,
                "location_ids": location_ids
            }
            all_inventory_levels = []
            while True:
                response = requests.get(url, headers=headers, params=params)
                if response.status_code == 200 and response.content:
                    inv_levels = response.json()
                    levels = inv_levels.get('inventory_levels', [])
                    all_inventory_levels.extend(levels)
                    page_info = inv_levels.get('page_info', {})
                    if 'has_next_page' in page_info and page_info['has_next_page']:
                        params['page_info'] = page_info['next_page']
                    else:
                        break
                else:
                    break
            if all_inventory_levels:
                updated_products = self.update_product_stock(all_inventory_levels, shopify_instance_id)
                return updated_products
            else:
                _logger.info("Inventory Levels not found in shopify store")
                return []

    def get_inventory_url(self, shopify_instance_id, endpoint):
        shop_url = "https://{}.myshopify.com/admin/api/{}/{}".format(shopify_instance_id.shopify_host,
                                                                     shopify_instance_id.shopify_version, endpoint)
        return shop_url

    def update_product_stock(self, levels, shopify_instance_id):
        stock_inventory_obj = self.env["stock.quant"]
        stock_inventory_name_obj = self.env["stock.inventory.adjustment.name"]
        warehouse_id = self.env['stock.warehouse'].sudo().search([('id', '=', 1)], limit=1)

        product_list = []
        stock_inventory_array = {}
        product_ids_list = []
        for level in levels:
            end_url = "inventory_items/{}.json".format(level.get('inventory_item_id'))
            url = self.get_inventory_url(shopify_instance_id, endpoint=end_url)
            access_token = shopify_instance_id.shopify_shared_secret
            headers = {
                "X-Shopify-Access-Token": access_token,
            }
            params = {
                "limit": 250,  # Adjust the page size as needed
            }

            response = requests.get(url, headers=headers, params=params)
            if response.status_code == 200 and response.content:
                inv_item = response.json()
                item = inv_item.get('inventory_item', [])
                if item.get('sku'):
                    product = self.env['product.product'].sudo().search(
                        [('default_code', '=', item.get('sku'))], limit=1)
                    if product and level.get('available') != None and product not in product_ids_list:
                        stock_inventory_line = {
                            product.id: level.get('available'),
                        }
                        stock_inventory_array.update(stock_inventory_line)
                        product_ids_list.append(product)
                        product_list.append(product.id)
        inventory_name = 'Inventory For Instance "%s"' % (shopify_instance_id.name)
        inventories = stock_inventory_obj.create_inventory_adjustment_ept(stock_inventory_array,
                                                                          warehouse_id.lot_stock_id, True,
                                                                          inventory_name)
        return product_list

    def get_locations(self):
        locations = self.env['shopify.location'].sudo().search([('is_shopify', '=', True)])
        loc_ids = ','.join(str(loc.shopify_location_id) for loc in locations) if locations else ''
        return loc_ids

    def _iter_export_items(self, products, split_by_color):
        """Yield (product, color_value) pairs for export.

        If ``split_by_color`` is True and the product contains a color attribute,
        each color value is returned separately. Otherwise ``color_value`` will be
        ``None`` and the caller should treat the product as a single item.
        """
        for product in products:
            if split_by_color:
                color_line = product.attribute_line_ids.filtered(
                    lambda l: l.attribute_id.name.lower() == 'color'
                )
                if color_line:
                    for value in color_line.product_template_value_ids:
                        yield product, value
                    continue
            yield product, None

    def _get_option_attr_lines(self, product, instance_id):
        """Return a mapping of Shopify option positions to attribute lines."""
        attr_lines = list(product.attribute_line_ids)
        color_line = next((l for l in attr_lines if l.attribute_id.name.lower() == 'color'), None)
        size_line = next((l for l in attr_lines if l.attribute_id.name.lower() in ('size', 'talla')), None)
        other_lines = [l for l in attr_lines if l not in (color_line, size_line)]

        pos_map = {}
        if color_line:
            pos_map[instance_id.color_option_position] = color_line
        if size_line:
            pos_map[instance_id.size_option_position] = size_line
        if other_lines:
            pos_map[3] = other_lines[0]

        return pos_map

        
    def export_products_to_shopify(self, shopify_instance_ids, update=False, products=None, create_new=True):
        """
        Exporta productos a Shopify, filtrando por aquellos modificados desde la última exportación.
        """
        color_attribute = None
        for attr in self.env['product.attribute'].search([]):
            if attr.name and attr.name.lower().find('color') != -1:
                color_attribute = attr.name
                break
                
        if not shopify_instance_ids:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active', '=', True)])
            
        for instance_id in shopify_instance_ids:
            # Verificar que el último producto relevante de Shopify esté mapeado
            try:
                mapping_ok = self._check_last_shopify_product_map(instance_id)
            except UserError as e:
                _logger.error(f"WSSH Exportación de productos abortada para instancia {instance_id.name} debido a error en verificación inicial: {e}")
                return

            if not mapping_ok:
                _logger.warning(
                    "WSSH No se encontró mapeo para el último producto relevante. "
                    "Importando productos recientes para recrearlo..."
                )
                self.import_shopify_products_sub(
                    instance_id,
                    skip_existing_products=True,
                    from_date=False,
                    to_date=fields.Datetime.now().isoformat(),
                    sort_order="desc",
                )
            
            export_update_time = fields.Datetime.now()                 
            
            # Si products está informado, usarlo; de lo contrario, buscar productos publicados
            if products is not None:
                _logger.info(f"WSSH Seleccion manual Exportacion {len(products)}")
                products_to_export = products
            else:
                domain = [('is_published', '=', True)]
                last_product_id = instance_id.last_export_product_id if instance_id.last_export_product_id and instance_id.last_export_product_id > 0 else 0
                if instance_id.last_export_product or last_product_id>0:
                    _logger.info(f"WSSH Starting product export id {last_product_id} y fecha {instance_id.last_export_product} instance {instance_id.name} atcolor {color_attribute}") 
                    #ojo cuando id>0 tambien debe filtrar por write_date , que no debe modificarse desde la ultima vez, si no filtra por ello la tanda de ids pendientes seria otra
                    domain.append(('write_date', '>=', instance_id.last_export_product  or '1900-01-01 00:00:00'))
                    domain.append(('id', '>', last_product_id))

                order = "id asc"

                products_to_export = self.search(domain, order=order)
                
            product_count = len(products_to_export)
            _logger.info("WSSH Found %d products to export for instance %s", product_count, instance_id.name)
        
            if not products_to_export:
                _logger.info("WSSH No products to export for instance %s", instance_id.name)
                continue

            headers = {
                "X-Shopify-Access-Token": instance_id.shopify_shared_secret,
                "Content-Type": "application/json"
            }


            processed_count = 0
            max_processed = 100  # Limitar a 10 productos exportados por ejecución
            last_request_time = time.time() - 1  # Para permitir la primera llamada inmediata

            for product, color_value in self._iter_export_items(products_to_export, instance_id.split_products_by_color):
                product_processed = False

                if color_value:
                    _logger.info(f"WSSH Exporta color {color_value.name} for product {product.id}")
                else:
                    _logger.info(f"WSSH Exporta no split v2 para id {product.id}")

                response = None
                # Filtrar variantes según corresponda
                variants = product.product_variant_ids.filtered(
                    lambda v: (color_value in v.product_template_attribute_value_ids if color_value else True)
                              and v.barcode
                )

                if not variants and products is None:
                    name = color_value.name if color_value else 'N/A'
                    _logger.info(f"WSSH No hay variantes con codigo {name}")
                    continue

                # Verificar si hay nuevas variantes sin mapeo
                new_variants = variants.filtered(
                    lambda v: not v.shopify_variant_map_ids.filtered(lambda m: m.shopify_instance_id == instance_id)
                )

                # CORREGIDO: Usar el método existente para obtener option_attr_lines correctamente
                base_option_attr_lines = self._get_option_attr_lines(product, instance_id)

                # Obtener product_map antes del bucle de variantes
                product_map = (
                    color_value.shopify_product_map_ids if color_value else product.shopify_product_map_ids
                ).filtered(lambda m: m.shopify_instance_id == instance_id)

                # Preparar datos de variantes usando las líneas de atributos correctas
                variant_data = []
                for variant in variants:
                    if variant.default_code:
                        # CORREGIDO: Cada variante decide individualmente si es update o creación
                        # basándose en si la variante específica ya existe en Shopify
                        variant_map = variant.shopify_variant_map_ids.filtered(
                            lambda m: m.shopify_instance_id == instance_id
                        )
                        variant_exists_in_shopify = bool(variant_map and variant_map.web_variant_id)

                        variant_result = self._prepare_shopify_variant_data(
                            variant, instance_id, is_update=variant_exists_in_shopify
                        )
                        variant_data.append(variant_result)

                # Ordenar variantes por talla si existe línea de talla
                size_pos = instance_id.size_option_position
                if size_pos in base_option_attr_lines:
                    variant_data.sort(
                        key=lambda v: get_size_value(
                            v.get(f"option{size_pos}", "")
                        )
                    )

                for position, variant in enumerate(variant_data, 1):
                    variant["position"] = position

                if not variant_data:
                    cname = color_value.name if color_value else 'N/A'
                    _logger.info("WSSH Skipping Shopify export for product '%s' with color '%s' because no variant has default_code",
                                 product.name, cname)
                    continue

                # CORREGIDO: Construir opciones usando directamente option_attr_lines (ya ordenado por posición)
                options_data = []

                for position in sorted(base_option_attr_lines):
                    attr_line = base_option_attr_lines[position]

                    if position == instance_id.color_option_position:
                        if color_value:
                            color_values = [color_value.name]
                        else:
                            color_values = sorted(set(v.get(f"option{position}", "") for v in variant_data))
                        options_data.append({
                            "name": "Color",
                            "position": position,
                            "values": color_values
                        })
                    elif position == instance_id.size_option_position:
                        cname = color_value.name if color_value else 'N/A'
                        _logger.info("WSSH variant_data antes de construir size_values para producto '%s', color '%s': %s", product.name, cname, variant_data)
                        # Extraer valores únicos preservando el orden (variant_data ya está ordenado por talla)
                        size_values = []
                        seen = set()
                        for v in variant_data:
                            _logger.info("WSSH Variante para options: %s", v)
                            size_val = v.get(f"option{position}", "")

                            _logger.info("WSSH checking talla para variante SKU=%s => '%s'", v.get('sku', ''), size_val)
                            if not size_val:
                                _logger.error("WSSH ERROR: Variante con valor de talla vacío en producto '%s', color '%s', variante: %s", product.name, cname, v)
                                raise UserError(f"Error: Hay al menos una variante con valor de talla vacío para el producto '{product.name}' y color '{cname}'. Corrige los datos antes de exportar.")

                            if size_val not in seen:
                                size_values.append(size_val)
                                seen.add(size_val)
                        _logger.info("WSSH size_values construidos para producto '%s', color '%s': %s", product.name, cname, size_values)

                        if not size_values:
                            _logger.error("WSSH ERROR: No se detectaron valores válidos de talla para el producto %s y color %s", product.name, cname)
                            raise UserError(f"Error: No se detectaron valores válidos de talla para el producto '{product.name}' y color '{cname}'.")

                        options_data.append({
                            "name": "Talla",
                            "position": position,
                            "values": size_values
                        })
                    else:
                        # Otros atributos
                        other_values = sorted(set(v.get(f"option{position}", "") for v in variant_data))
                        options_data.append({
                            "name": attr_line.attribute_id.name,
                            "position": position,
                            "values": other_values
                        })

                product_data = {
                    "product": {
                        "options": options_data,
                        "tags": ','.join(tag.name for tag in product.product_tag_ids),
                        "variants": variant_data
                    }
                }

                # Logging para debugging
                _logger.info(f"WSSH DEBUG - Product options: {[opt['name'] + ':' + str(opt['position']) for opt in options_data]}")
                _logger.info(f"WSSH DEBUG - First variant options: {[(k, v) for k, v in variant_data[0].items() if k.startswith('option')] if variant_data else 'No variants'}")

                if product_map:
                    if update or create_new and len(new_variants) > 0:
                        product_data["product"]["id"] = product_map.web_product_id
                        url = self.get_products_url(instance_id, f'products/{product_map.web_product_id}.json')
                        _logger.info(f"WSSH Updating Shopify product {product_map.web_product_id} {instance_id.name}")
                        # --- LOG DEL PAYLOAD ---
                        _logger.info("WSSH PAYLOAD FINAL ENVIADO A SHOPIFY:\n%s", json.dumps(product_data, indent=2, ensure_ascii=False))

                        
                        try:
                            retries = 0
                            while True:
                                # Respetar el rate limit de Shopify
                                elapsed = time.time() - last_request_time
                                if elapsed < 0.5:
                                    sleep_time = 0.5 - elapsed
                                    _logger.info(f"WSSH Esperando {sleep_time}s para respetar rate limits")
                                    time.sleep(sleep_time)

                                response = requests.put(url, headers=headers, data=json.dumps(product_data))
                                last_request_time = time.time()
                                _logger.info(f"WSSH PUT request status: {response.status_code}")

                                if response.ok:
                                    _logger.info(f"WSSH Response Ok - Updated product {product_map.web_product_id}")
                                    product_processed = True
                                    break

                                if response.status_code == 422 and retries == 0:
                                    try:
                                        err_data = response.json()
                                        variant_errors = err_data.get('errors', {}).get('variants', [])
                                        missing_ids = []
                                        removed_variants = self.env['product.product']

                                        for msg in variant_errors:
                                            m = re.search(r"\[([0-9 ,]+)\]", msg)
                                            if m:
                                                ids = [v.strip() for v in m.group(1).split(',') if v.strip()]
                                                missing_ids.extend(ids)
                                                for vid in ids:
                                                    maps = self.env['shopify.variant.map'].sudo().search([
                                                        ('web_variant_id', '=', vid),
                                                        ('shopify_instance_id', '=', instance_id.id),
                                                    ])
                                                    if maps:
                                                        removed_variants |= maps.mapped('odoo_id')
                                                        maps.unlink()
                                                        _logger.warning(
                                                            f"WSSH Removed mapping for deleted variant {vid} on instance {instance_id.name}")
                                        if missing_ids:
                                            # Remove missing variants from payload and local lists
                                            variant_data[:] = [
                                                v for v in variant_data if str(v.get('id')) not in missing_ids
                                            ]
                                            variants -= removed_variants
                                            new_variants -= removed_variants
                                            product_data['product']['variants'] = variant_data
                                            retries += 1
                                        _logger.info(
                                                f"WSSH Retrying update without variants {missing_ids}")
                                        continue
                                    except Exception as parse_e:
                                        _logger.error(f"WSSH Error processing 422 response: {str(parse_e)}")

                                if response.status_code == 429:
                                    retry_after = int(response.headers.get('Retry-After', 1))
                                    _logger.warning(f"WSSH Rate limit alcanzado. Esperando {retry_after}s antes de reintentar...")
                                    time.sleep(retry_after)
                                    continue

                                if response.status_code == 404:
                                    # Shopify product no longer exists
                                    self._handle_missing_shopify_product(product, instance_id, product_map)
                                    product_processed = True
                                    break

                                _logger.error(
                                    f"WSSH Error updating product {product_map.web_product_id}: Status {response.status_code}, Response: {response.text}")
                                cname = color_value.name if color_value else 'N/A'
                                raise UserError(
                                    f"WSSH Error updating product {product.name} - {cname}: {response.text}")
                            # end while

                        except requests.exceptions.RequestException as e:
                            _logger.error(f"WSSH Network error updating product {product_map.web_product_id}: {str(e)}")
                            cname = color_value.name if color_value else 'N/A'
                            raise UserError(f"WSSH Network error updating product {product.name} - {cname}: {str(e)}")
                        except Exception as e:
                            _logger.error(f"WSSH Unexpected error updating product {product_map.web_product_id}: {str(e)}")
                            cname = color_value.name if color_value else 'N/A'
                            raise UserError(f"WSSH Unexpected error updating product {product.name} - {cname}: {str(e)}")
                    else:
                        _logger.info(f"WSSH Ignorar, por no update, Shopify product {product_map.web_product_id}")

                elif create_new:
                    cname = color_value.name if color_value else ''
                    _logger.info(f"WSSH creando {product.name}{' - ' + cname if cname else ''}")
                    product_data["product"]["title"] = f"{product.name}{' - ' + cname if cname else ''}"
                    product_data["product"]["status"] = 'draft'
                    if product.description:
                        product_data["product"]["body_html"] = product.description

                    url = self.get_products_url(instance_id, 'products.json')

                    try:
                        # Respetar el rate limit de Shopify
                        elapsed = time.time() - last_request_time
                        if elapsed < 0.5:
                            sleep_time = 0.5 - elapsed
                            _logger.info(f"WSSH Esperando {sleep_time}s para respetar rate limits")
                            time.sleep(sleep_time)

                        response = requests.post(url, headers=headers, data=json.dumps(product_data))
                        last_request_time = time.time()
                        _logger.info(f"WSSH POST request status: {response.status_code}")

                        if response.ok:
                            _logger.info(f"WSSH Response Ok - Created new product")
                            product_processed = True
                            shopify_product = response.json().get('product', {})
                            if shopify_product:
                                # Crear el mapeo del producto
                                if color_value:
                                    self.env['shopify.product.map'].create({
                                        'web_product_id': shopify_product.get('id'),
                                        'odoo_id': color_value.id,
                                        'shopify_instance_id': instance_id.id,
                                    })
                                else:
                                    self.env['shopify.product.template.map'].create({
                                        'web_product_id': shopify_product.get('id'),
                                        'odoo_id': product.id,
                                        'shopify_instance_id': instance_id.id,
                                    })
                                _logger.info(f"WSSH Created product map for Shopify product ID: {shopify_product.get('id')}")
                            else:
                                _logger.warning(f"WSSH No product data in successful response")
                        elif response.status_code == 429:
                            retry_after = int(response.headers.get('Retry-After', 1))
                            _logger.warning(f"WSSH Rate limit alcanzado. Esperando {retry_after}s antes de reintentar...")
                            time.sleep(retry_after)
                            continue
                        else:
                            _logger.error(f"WSSH Error creating product: Status {response.status_code}, Response: {response.text}")
                            cname = color_value.name if color_value else 'N/A'
                            raise UserError(f"WSSH Error creating product {product.name} - {cname}: {response.text}")

                    except requests.exceptions.RequestException as e:
                        _logger.error(f"WSSH Network error creating product: {str(e)}")
                        cname = color_value.name if color_value else 'N/A'
                        raise UserError(f"WSSH Network error creating product {product.name} - {cname}: {str(e)}")
                    except json.JSONDecodeError as e:
                        _logger.error(f"WSSH JSON decode error in response: {str(e)}, Response text: {response.text if response else 'No response'}")
                        cname = color_value.name if color_value else 'N/A'
                        raise UserError(f"WSSH Invalid JSON response from Shopify for product {product.name} - {cname}")
                    except Exception as e:
                        _logger.error(f"WSSH Unexpected error creating product: {str(e)}")
                        cname = color_value.name if color_value else 'N/A'
                        raise UserError(f"WSSH Unexpected error creating product {product.name} - {cname}: {str(e)}")

                # Procesar respuesta y actualizar variant IDs si todo fue exitoso
                if response and response.ok:
                    try:
                        shopify_product = response.json().get('product', {})
                        if shopify_product:
                            shopify_variants = shopify_product.get('variants', [])
                            self._update_variant_ids(variants, shopify_variants, instance_id)
                            cname = color_value.name if color_value else 'N/A'
                            _logger.info(f"WSSH Updated variant IDs for {len(shopify_variants)} variants for color {cname}")
                        else:
                            cname = color_value.name if color_value else 'N/A'
                            _logger.warning(f"WSSH No product data in response for {cname}")
                    except json.JSONDecodeError as e:
                        _logger.error(f"WSSH Error parsing successful response JSON: {str(e)}")
                    except Exception as e:
                        _logger.error(f"WSSH Error processing successful response: {str(e)}")
                elif response:
                    # Este caso ya se manejó arriba con raise UserError
                    pass
                else:
                    cname = color_value.name if color_value else 'N/A'
                    _logger.warning(f"WSSH No response object for color {cname} - this should not happen")

                # Logging adicional para debugging
                if response:
                    cname = color_value.name if color_value else 'N/A'
                    _logger.info(f"WSSH Final response status for {cname}: {response.status_code}")
                else:
                    cname = color_value.name if color_value else 'N/A'
                    _logger.warning(f"WSSH No response object created for {cname}")

                if product_processed:
                    processed_count += 1
                    self.write_with_retry(instance_id, 'last_export_product_id', product.id)

                if processed_count >= max_processed:
                    _logger.info("WSSH Processed %d products for instance %s. Stopping export for this run.", processed_count, instance_id.name)

                    export_update_time = product.write_date - datetime.timedelta(seconds=1)
                    break
            # Corregir lógica de actualización de last_export_product                                          
            if processed_count > 0:
                if processed_count < max_processed:
                    # Si se procesaron todos los productos pendientes, actualizar fecha y resetear ID
                    self.write_with_retry(instance_id, 'last_export_product', export_update_time)
                    self.write_with_retry(instance_id, 'last_export_product_id', 0)
                else:
                    # Si se alcanzó el límite, actualizar fecha hasta el último producto procesado
                    self.write_with_retry(instance_id, 'last_export_product', export_update_time)
                               
            
    def _update_variant_ids(self, odoo_variants, shopify_variants, instance_id):
        """
        Actualiza los IDs de las variantes de Shopify en las variantes de Odoo, 
        y actualiza los mappings de stock basados en la variante y la ubicación en lugar de stock.quant.
        """
        shopify_location = self.env['shopify.location'].sudo().search([
            ('shopify_instance_id', '=', instance_id.id)
        ], limit=1)
        
        for odoo_variant in odoo_variants:
            matched_shopi_variant = None
            # Buscar coincidencia en shopify_variants usando SKU o barcode
            for shopify_variant in shopify_variants:
                # Versión más detallada
                shopify_sku = shopify_variant.get('sku')
                shopify_barcode = shopify_variant.get('barcode')
                odoo_sku = odoo_variant.default_code
                odoo_barcode = odoo_variant.barcode
                
                if shopify_sku     and (shopify_sku == odoo_sku)         or \
                   shopify_barcode and (shopify_barcode == odoo_barcode):
                    matched_shopi_variant = shopify_variant
                    #_logger.info(f"WSSH Match encontrado: Shopify SKU/Barcode: {shopify_sku}/{shopify_barcode} coincide con Odoo SKU/Barcode: {odoo_sku}/{odoo_barcode}")
                    break
    
            if matched_shopi_variant:
                # Actualizar o crear el mapeo de la variante
                variant_map = odoo_variant.shopify_variant_map_ids.filtered(
                    lambda m: m.shopify_instance_id == instance_id
                )
                if variant_map:
                    if variant_map.web_variant_id != matched_shopi_variant.get('id'):
                        variant_map.write({'web_variant_id': matched_shopi_variant.get('id')})
                        #_logger.info("Updated variant map for Odoo variant (SKU: %s)", odoo_variant.default_code)
                else:
                    self.env['shopify.variant.map'].create({
                        'web_variant_id': matched_shopi_variant.get('id'),
                        'odoo_id': odoo_variant.id,
                        'shopify_instance_id': instance_id.id,
                    })
                    #_logger.info("Created variant map for Odoo variant (SKU: %s)", odoo_variant.default_code)
    
                # Actualizar o crear el mapeo de stock basado en la variante y la ubicación
                if shopify_location:
                    stock_map = self.env['shopify.stock.map'].sudo().search([
                        ('odoo_id', '=', odoo_variant.id),
                        ('shopify_instance_id', '=', instance_id.id),
                        ('shopify_location_id', '=', shopify_location.id)
                    
                    ], limit=1)
                                   
                    if stock_map:                                                                                                                                        
                        if stock_map.web_stock_id != matched_shopi_variant.get('inventory_item_id'):
                            stock_map.write({'web_stock_id': matched_shopi_variant.get('inventory_item_id')})
                            #_logger.info("Updated stock map for Odoo variant (SKU: %s)", odoo_variant.default_code)                                                                                  
                    else:
                        self.env['shopify.stock.map'].create({
                            'web_stock_id': matched_shopi_variant.get('inventory_item_id'),
                            'odoo_id': odoo_variant.id,
                            'shopify_instance_id': instance_id.id,
                            'shopify_location_id': shopify_location.id,
                        })
                        #_logger.info("Created stock map for Odoo variant (SKU: %s)", odoo_variant.default_code)
                                                                                         
                else:
                    _logger.warning("No shopify.location found for instance %s", instance_id.name)
            else:
                sku = odoo_variant.default_code or 'N/A'
                _logger.warning(f"WSSH Update var: No matching Shopify variant found for Odoo variant with SKU %s", sku)


    def _handle_missing_shopify_product(self, product, instance_id, product_map):
        """Handle a 404 error when trying to update a Shopify product.

        This method logs the incident, unpublishes the product in Odoo,
        appends the marker ``-ELIMINADO SHOPIFY`` to the description and
        removes all mapping records related to the product, its variants and
        stock mappings.
        """

        _logger.error(
            "WSSH Shopify product %s not found for instance %s. Marking as deleted.",
            product_map.web_product_id if product_map else 'N/A',
            instance_id.name,
        )

        # Mark product as unpublished and update description
        description = product.description or ""
        if "-ELIMINADO SHOPIFY" not in description:
            description = (description + " -ELIMINADO SHOPIFY").strip()
        product.sudo().write({
            "is_published": False,
            "description": description,
        })

        # Remove template/product mapping
        if product_map:
            product_map.unlink()

        # Remove variant and stock mappings for this instance
        variant_maps = product.product_variant_ids.mapped("shopify_variant_map_ids").filtered(
            lambda m: m.shopify_instance_id == instance_id
        )
        stock_maps = product.product_variant_ids.mapped("shopify_stock_map_ids").filtered(
            lambda m: m.shopify_instance_id == instance_id
        )
        if variant_maps:
            variant_maps.unlink()
        if stock_maps:
            stock_maps.unlink()

                
    def _prepare_shopify_variant_data(self, variant, instance_id, is_update=False):
        """
        Prepara los datos de una variante para Shopify, usando las posiciones configuradas para color y talla.
        """
        # Posición de las opciones según configuración de la instancia
        color_pos = instance_id.color_option_position if hasattr(instance_id, 'color_option_position') else 1
        size_pos = instance_id.size_option_position if hasattr(instance_id, 'size_option_position') else 2

        # Extraer valores reales de color y talla desde la variante
        color_val = None
        size_val = None
        for val in variant.product_template_attribute_value_ids:
            attr_name = val.attribute_id.name.lower()
            if attr_name == 'color':
                color_val = val.name
            elif attr_name in ('size', 'talla'):
                size_val = val.name

        # Comprobación de errores: si falta algún valor requerido, lanzar excepción
        if not color_val:
            _logger.error("WSSH ERROR: Variante SKU %s no tiene color asignado.", variant.default_code)
            raise UserError(f"Error: Variante SKU {variant.default_code} no tiene color asignado.")
        if not size_val:
            _logger.error("WSSH ERROR: Variante SKU %s no tiene talla asignada.", variant.default_code)
            raise UserError(f"Error: Variante SKU {variant.default_code} no tiene talla asignada.")

        # Construir el diccionario resultado para Shopify
        result = {
            "price": variant.product_tmpl_id.wholesale_price if not instance_id.prices_include_tax else variant.list_price,
            'sku': variant.default_code,
            'barcode': variant.barcode,
            'inventory_management': 'shopify',
        }

        result[f'option{color_pos}'] = color_val
        result[f'option{size_pos}'] = size_val

        # Si es update y el variant ya existe, incluye su id
        if is_update:
            variant_map = variant.shopify_variant_map_ids.filtered(
                lambda m: m.shopify_instance_id == instance_id
            )
            if variant_map and variant_map.web_variant_id:
                result['id'] = str(variant_map.web_variant_id)

        return result

        
    def _extract_name(self, attr_val):
        """
        Si attr_val.name tiene el formato 'code:name' y el código coincide con attr_val.code,
        devuelve solo la parte después de ':'; en caso contrario, devuelve attr_val.name.
        """
        
        if not attr_val.name:
            return ""
        m = re.match(r'([^:]+):(.+)', attr_val.name)
        if m and m.group(1) == (attr_val.code or ""):
            return m.group(2)
        return attr_val.name
            
    def export_stock_to_shopify(self, shopify_instance_ids, products=None):
        _logger.info("WSSH Exportar stocks")
        
        if not shopify_instance_ids:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active', '=', True)])
       
        for shopify_instance in shopify_instance_ids: 
            updated_ids = []
            location = self.env['shopify.location'].sudo().search(
                [('shopify_instance_id', '=', shopify_instance.id)], limit=1)
            
            if not location:
                _logger.warning("No shopify.location found for instance %s", shopify_instance.name)
                return updated_ids
            
            # Definir ubicación a usar: específica o la primera interna
            if location.import_stock_warehouse_id:
                # Si hay ubicación específica configurada, usar esa
                internal_location = location.import_stock_warehouse_id
                _logger.info("WSSH Usando ubicación específica configurada: %s", internal_location.name)
            else:
                # Si no hay ubicación específica, usar la primera ubicación interna
                internal_location = self.env['stock.location'].sudo().search([
                    ('usage', '=', 'internal'),
                    ('company_id', '=', self.env.company.id)
                ], limit=1)
                if internal_location:
                    _logger.info("WSSH Usando primera ubicación interna: %s", internal_location.name)
                else:
                    _logger.warning("WSSH No se encontraron ubicaciones internas para la instancia %s", shopify_instance.name)
            
            last_stock_id = shopify_instance.last_export_stock_id if shopify_instance.last_export_stock_id and shopify_instance.last_export_stock_id > 0 else 0
            # Si se pasa una selección de productos, se filtran las variantes correspondientes.
            if products:
                # Buscamos las variantes asociadas a los templates seleccionados.
                variants = self.env['product.product'].search([('product_tmpl_id', 'in', products.ids)])
            else:
                # Dominio original para stock.quants, según fecha y último ID exportado.
                # usamos effective_export_date (que es el mayor entre fecha escritura quant y fecha creacion mapa stock)
                # porque entre la creacion de productos en odoo y la tarea de exportacion de productos que crea el mapa de stock 
                # se ha podido colar la tarea de exportacion de stock, retrasando la ultima fecha de exportacion de stock 
                # saltandose los quant modificados
                quant_domain = [
                    ('effective_export_date', '>=', shopify_instance.last_export_stock or '1900-01-01 00:00:00')
                ]
                
                # Solo aplicar filtro por product_id si last_stock_id > 0
                if last_stock_id > 0:
                    quant_domain.append(('product_id', '>', last_stock_id))
                    
                if internal_location:
                    # Usar la ubicación definida (específica o primera interna)
                    quant_domain.append(('location_id', '=', internal_location.id))
                
                # Buscar stock.quants con el orden apropiado por product_id
                order = "product_id asc"
    
                quants = self.env['stock.quant'].sudo().search(quant_domain, order=order)
                # Obtener variantes únicas de los quants (mapped elimina duplicados automáticamente)
                variants = quants.mapped('product_id').sorted('id')
            
            # Filtrar variantes con shopify_stock_map_ids válidos para la instancia y ubicación actuales
            variants = variants.filtered(lambda v: any(
                m.shopify_instance_id == shopify_instance and m.web_stock_id and m.shopify_location_id == location
                for m in v.shopify_stock_map_ids
            ))
            
            _logger.info("WSSH Found %s variants para la instancia %s", len(variants), shopify_instance.name)
            
            # Control de tiempo entre peticiones
            last_query_time = time.time()
            iteration_timeout = 250
            iteration_start_time = time.time()
            
            for variant in variants:
                stock_map = variant.shopify_stock_map_ids.filtered(lambda m: m.shopify_instance_id == shopify_instance)
                if not stock_map or not stock_map.web_stock_id:
                    continue
                
                # CORRECCIÓN: Obtener cantidad disponible correctamente
                if internal_location:
                    # Calcular stock disponible en la ubicación definida
                    # (ya sea la específica configurada o la primera interna)
                    # SUMA todos los quants/lotes de la variante - esto es coherente con el mapa agregado
                    quant_qty = self.env['stock.quant'].sudo().search([
                        ('product_id', '=', variant.id),
                        ('location_id', '=', internal_location.id)
                    ])
                    available_qty = sum(q.quantity for q in quant_qty if q.quantity > 0)
                else:
                    # Fallback: usar qty_available general pero limitado a stock positivo
                    available_qty = max(0, variant.qty_available)
                
                # CORRECCIÓN ADICIONAL: Asegurar que no se envíen cantidades negativas
                # Shopify no maneja bien las cantidades negativas
                available_qty = max(0, available_qty)
                
                _logger.info("WSSH Enviando stock para variante %s (SKU: %s): %s a Shopify", 
                            variant.id, variant.default_code, available_qty)
                
                # Control de tiempo entre peticiones
                elapsed = time.time() - last_query_time
                if elapsed < 0.5:
                    time.sleep(0.5 - elapsed)
                last_query_time = time.time()
                
                # Enviar solicitud a Shopify
                url = self.get_products_url(shopify_instance, 'inventory_levels/set.json')
                headers = {
                    "X-Shopify-Access-Token": shopify_instance.shopify_shared_secret,
                    "Content-Type": "application/json"
                }
                data_payload = {
                    "location_id": location.shopify_location_id,
                    "inventory_item_id": stock_map.web_stock_id,
                    "available": int(available_qty),
                }
                
                response = requests.post(url, headers=headers, json=data_payload)
                if response.status_code in (200, 201):
                    updated_ids.append(variant.id)
                    # Actualizar con el ID de la variante procesada (consistente con el mapa agregado)
                    self.write_with_retry(shopify_instance, 'last_export_stock_id', variant.id)
                else:
                    _logger.warning("WSSH Failed to update stock for product %s (variant %s): %s en instancia %s",
                                    variant.product_tmpl_id.name, variant.name, response.text, shopify_instance.name)
                
                # Verificar timeout
                if time.time() - iteration_start_time > iteration_timeout:
                    _logger.error("WSSH Timeout de iteración alcanzado para el producto %s en instancia %s",
                                  variant.default_code, shopify_instance.name)
                    return updated_ids
            
            # Si se procesan todos los productos, actualizar a la fecha actual y resetear el ID de variante
            _logger.info("WSSH Update stock final completo para %s", shopify_instance.name)
            self.write_with_retry(shopify_instance, 'last_export_stock', fields.Datetime.now())
            self.write_with_retry(shopify_instance, 'last_export_stock_id', 0)
            return updated_ids

    def _check_last_shopify_product_map(self, shopify_instance):
        """
        Comprueba si el producto más reciente de Shopify con variantes y código
        de barras tiene un mapeo en Odoo.

        Devuelve ``True`` si existe el mapeo y ``False`` en caso contrario.
        Cualquier error de conexión genera una :class:`UserError`.
        """
        _logger.info("WSSH Verificando mapeo del último producto *relevante* (con barcode) creado en Shopify para instancia %s",
                     shopify_instance.name)

        try:
            # 1. Construir la URL y headers para la llamada a la API de Shopify
            endpoint = 'products.json'
            url = self.get_products_url(shopify_instance, endpoint)
            access_token = shopify_instance.shopify_shared_secret
            headers = {
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json" # Aunque es GET, es buena práctica incluirlo a veces
            }

            # Parámetros para la solicitud: obtener los últimos N productos creados
            fetch_limit = 10 # Podrías ajustar este número
            params = {
                'order': 'created_at desc', # Ordenar por fecha de creación descendente
                'limit': fetch_limit        # Limitar el número de resultados
            }

            # 2. Realizar la llamada GET a la API de Shopify
            response = requests.get(url, headers=headers, params=params)

            # 3. Manejar la respuesta de la API
            if response.status_code != 200:
                _logger.error(
                    "WSSH Error al obtener los últimos %s productos de Shopify para instancia %s. "
                    "Código de estado: %s, Respuesta: %s",
                    fetch_limit, shopify_instance.name, response.status_code, response.text
                )
                # Lanzar una excepción detendrá la tarea cron
                raise UserError(_(f"Error al conectar con Shopify para verificar los últimos productos ({shopify_instance.name}). Código: {response.status_code}"))

            # Intentar parsear la respuesta JSON
            try:
                last_products_data = response.json().get('products')
            except json.JSONDecodeError:
                 _logger.error(
                    "WSSH Error al decodificar la respuesta JSON al obtener los últimos %s productos de Shopify para instancia %s. "
                    "Respuesta: %s",
                    fetch_limit, shopify_instance.name, response.text
                )
                 raise UserError(_(f"Respuesta inválida de Shopify al verificar productos ({shopify_instance.name})."))


            if not last_products_data:
                _logger.info("WSSH No se encontraron productos recientes en Shopify para la instancia %s. Procediendo...",
                             shopify_instance.name)
                return True # No hay productos recientes, no hay nada que verificar, se asume seguro proceder

            # 4. Buscar el primer producto *relevante* (con variantes y barcode) dentro de los obtenidos
            first_relevant_shopify_product = None
            for shopify_product in last_products_data:
                #shopify_product_id = str(shopify_product.get('id')) # Ya lo extraemos más adelante si es relevante
                variants = shopify_product.get('variants', [])

                # Criterio simplificado de relevancia:
                # - Tiene variantes (asegura que no es un producto simple sin variantes esperadas por el conector)
                # - Al menos una variante tiene código de barras (barcode)
                has_variants_with_barcode = any(v.get('barcode') for v in variants)

                # Si cumple el criterio simplificado
                if variants and has_variants_with_barcode:
                     first_relevant_shopify_product = shopify_product
                     # Extraer el ID aquí una vez que sabemos que es relevante
                     relevant_shopify_product_id = str(first_relevant_shopify_product.get('id'))
                     _logger.info("WSSH Encontrado primer producto *relevante* (con barcode) en Shopify: ID %s (instancia %s)",
                                  relevant_shopify_product_id, shopify_instance.name)
                     break # Encontramos el más reciente relevante, salimos del bucle

            if not first_relevant_shopify_product:
                _logger.info("WSSH No se encontraron productos *relevantes* (con barcode) entre los últimos %s creados en Shopify para la instancia %s. Procediendo...",
                             fetch_limit, shopify_instance.name)
                return True # No se encontró ningún producto que parezca exportado por nosotros (según este criterio), se asume seguro proceder

            # 5. Si se encontró un producto relevante, verificar su mapeo en Odoo
            # Ya tenemos relevant_shopify_product_id del bucle anterior

            # Consultar las tablas de mapeo en Odoo (igual que antes)
            template_map = self.env['shopify.product.template.map'].search([
                ('web_product_id', '=', relevant_shopify_product_id),   
                ('shopify_instance_id', '=', shopify_instance.id)
            ], limit=1)

            # Buscar en shopify.product.map (mapeo de Odoo attribute value/color a Producto Shopify)
            product_map = self.env['shopify.product.map'].search([
                ('web_product_id', '=', relevant_shopify_product_id),
                ('shopify_instance_id', '=', shopify_instance.id)
            ], limit=1)

            # 6. Verificar si se encontró el mapeo del producto relevante
            if not template_map and not product_map:
                _logger.warning(
                    "WSSH El último producto relevante creado en Shopify (ID %s) no tiene mapeo en Odoo",
                    relevant_shopify_product_id,
                )
                return False

            _logger.info("WSSH Mapeo encontrado para el último producto *relevante* (con barcode) de Shopify (ID %s). Procediendo con la exportación...",
                         relevant_shopify_product_id)
            return True # Mapeo del producto relevante encontrado, continuar con la exportación

        except requests.exceptions.RequestException as e:
            # Captura errores específicos de la librería requests (problemas de red, timeouts, etc.)
            _logger.error(
                 "WSSH Error de conexión al verificar el último producto de Shopify para instancia %s: %s",
                 shopify_instance.name, e, exc_info=True
             )
            raise UserError(_(f"Error de conexión con Shopify al verificar productos ({shopify_instance.name})."))
        except Exception as e:
            # Captura cualquier otro error inesperado
            _logger.error(
                "WSSH Ocurrió un error inesperado durante la verificación inicial de mapeos con Shopify para instancia %s: %s",
                shopify_instance.name, e, exc_info=True
            )
            raise UserError(_(f"Fallo inesperado en la verificación inicial de mapeos con Shopify ({shopify_instance.name}). Revise los logs."))

    def write_with_retry(self, record, field_name, value):
        """
        Escribe un valor en un campo de un registro con reintentos en caso de SerializationFailure.
        
        :param record: Registro en el que se escribirá (ej. shopify.web)
        :param field_name: Nombre del campo a actualizar (ej. 'last_export_product')
        :param value: Valor a escribir en el campo
        """
        from psycopg2 import errors
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self.env.cr.execute("BEGIN")
                record.write({field_name: value})
                self.env.cr.commit()
                _logger.info(f"WSSH Successfully wrote {field_name}={value} to record {record._name} (ID: {record.id})")
                return
            except errors.SerializationFailure as e:
                self.env.cr.rollback()
                if attempt < max_retries - 1:
                    _logger.warning(f"WSSH Serialization failure when writing {field_name}, retrying {attempt + 1}/{max_retries}")
                    time.sleep(5)
                    continue
                _logger.error(f"WSSH Failed to write {field_name} after {max_retries} retries: {str(e)}")
                raise UserError(f"Error de concurrencia persistente al escribir {field_name}: {str(e)}")

                     
        


# inherit class product.attribute and add fields for shopify
class ProductAttribute(models.Model):
    _inherit = 'product.attribute'

    is_shopify = fields.Boolean(string='Is Shopify?')
    shopify_instance_id = fields.Many2one('shopify.web', string='Shopify Instance')
    shopify_id = fields.Char(string='Shopify Attribute Id')


# inherit class product.attribute.value and add fields for shopify
class ProductAttributeValue(models.Model):
    _inherit = 'product.attribute.value'

    is_shopify = fields.Boolean(string='Is Shopify?')
    shopify_instance_id = fields.Many2one('shopify.web', string='Shopify Instance')
    shopify_id = fields.Char(string='Shopify Attribute Value Id')