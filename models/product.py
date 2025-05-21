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

    def import_shopify_products(self, shopify_instance_ids, skip_existing_products, from_date, to_date):
        if not shopify_instance_ids:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active', '=', True)])
        
        for shopify_instance_id in shopify_instance_ids:
            _logger.info("WSSH Starting product import for instance %s", shopify_instance_id.name)                                                                                                  
            url = self.get_products_url(shopify_instance_id, endpoint='products.json')
            access_token = shopify_instance_id.shopify_shared_secret
            headers = {
                "X-Shopify-Access-Token": access_token,
            }
            
            # Parámetros para la solicitud
            params = {
                "limit": 250,  # Ajustar el tamaño de la página según sea necesario
                "order": "id asc",
                "pageInfo": None,  
            }
            
            if from_date and to_date:
                params.update({
                    "created_at_min": from_date,
                    "created_at_max": to_date,
                })
            
            all_products = []
            while True:
                #_logger.info("WSSH Shopify POST response JSON: %s", json.dumps(response.json(), indent=4))
                response = requests.get(url, headers=headers, params=params)                
                if response.status_code == 200 and response.content:
                    shopify_products = response.json()
                    products = shopify_products.get('products', [])
                    all_products.extend(products)
                    _logger.info("WSSH All products fetched : %d", len(all_products))
                    # Verificar si hay más páginas                        
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
                # Procesar los productos importados
                products = self._process_imported_products(all_products, shopify_instance_id, skip_existing_products)
                return products
            else:
                _logger.info("WSSH No products found in Shopify store for instance %s", shopify_instance_id.name)
                return []

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
        
    def export_products_to_shopify(self, shopify_instance_ids, update=False, products=None):
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
            # Si _check_last_shopify_product_map lanza un UserError, la ejecución se detendrá aquí.
            try:
                self._check_last_shopify_product_map(instance_id)
            except UserError as e:
                # Si _check_last_shopify_product_map lanza UserError, no continuar con la exportación
                _logger.error(f"WSSH Exportación de productos abortada para instancia {instance_id.name} debido a fallo en verificación inicial.")
                                                                                                            
                            
                return # Salir del método para esta instancia
            
            export_update_time = fields.Datetime.now()            
            # Si products está informado, usarlo; de lo contrario, buscar productos publicados
            if products is not None:
                _logger.info(f"WSSH Seleccion manual Exportacion {len(products)}")
                products_to_export = products
            else:
                domain = [('is_published', '=', True)]
                if instance_id.last_export_product:
                    _logger.info(f"WSSH Starting product export por fecha {instance_id.last_export_product} instance {instance_id.name} atcolor {color_attribute}") 
                    domain.append(('write_date', '>', instance_id.last_export_product))

                products_to_export = self.search(domain, order='write_date')
                
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
            max_processed = 5  # Limitar a 10 productos exportados por ejecución
        
            for product in products_to_export:                
                if not instance_id.split_products_by_color:
                    _logger.info("WSSH Exporta no split v2")                 
                    self._export_single_product_v2(product, instance_id, headers, update)
                    processed_count += 1  # Cambio: Incrementar contador
                    continue

                color_line = product.attribute_line_ids.filtered(
                    lambda l: l.attribute_id.name.lower() == 'color')
                if not color_line:
                    _logger.info("WSSH Exporta no color line v2")                     
                    self._export_single_product_v2(product, instance_id, headers, update)
                    processed_count += 1  # Cambio: Incrementar contador
                    continue
                # Variable para rastrear si se procesó al menos una variante del producto
                product_processed = False                                                                                          
               
                for template_attribute_value in color_line.product_template_value_ids:
                                                                                                                                                       
                    response = None
                    # Filtrar variantes para este color
                    variants = product.product_variant_ids.filtered(
                        lambda v: template_attribute_value in v.product_template_attribute_value_ids 
                                  and v.barcode
                    )
                    
                    if not variants and products is None:
                        _logger.info(f"WSSH No hay variantes con codigo {template_attribute_value.name}")
                        continue

                    # Verificar si hay nuevas variantes sin mapeo
                    new_variants = variants.filtered(
                        lambda v: not v.shopify_variant_map_ids.filtered(lambda m: m.shopify_instance_id == instance_id)
                    )

                    # Construcción de option_attr_lines: primero color (fijo), luego talla, mantiene equivalencia con el comportamiento original
                    size_line = next((l for l in product.attribute_line_ids if l.attribute_id.name.lower() in ('size', 'talla')), None)
                    option_attr_lines = []
                    color_fixed_line = None
                    if color_line:
                        # Creamos una "línea ficticia" solo con el valor actual de split
                        color_fixed_line = color_line[0]
                        option_attr_lines.append(color_fixed_line)
                    if size_line:
                        option_attr_lines.append(size_line)
                    # --- FIN CONSTRUCCIÓN option_attr_lines ---

                    variant_data = [
                        self._prepare_shopify_variant_data(
                            variant, instance_id, option_attr_lines, color_value=template_attribute_value, is_update=update
                        )
                        for variant in variants
                        if variant.default_code
                    ]
                                                   
                    variant_data.sort(key=lambda v: get_size_value(v.get(f"option{instance_id.size_option_position}", "")))
                                                    
                    for position, variant in enumerate(variant_data, 1):
                        variant["position"] = position
                    
                                                                                          
                    if not variant_data:
                        _logger.info("WSSH Skipping Shopify export for product '%s' with color '%s' because no variant has default_code",
                                     product.name, template_attribute_value.name)
                        continue

                    product_data = {
                        "product": {                            
                            "options": [
                                {
                                    "name": "Color",
                                    "position": instance_id.color_option_position,
                                    "values": [template_attribute_value.name]
                                },
                                {
                                    "name": "Size",
                                    "position": instance_id.size_option_position,
                                    "values": sorted(set(v.get(f"option{instance_id.size_option_position}", "") for v in variant_data), key=get_size_value)
                                }
                            ],
                            "tags": ','.join(tag.name for tag in product.product_tag_ids),
                            "variants": variant_data
                        }
                    }

                    product_map = template_attribute_value.shopify_product_map_ids.filtered(
                        lambda m: m.shopify_instance_id == instance_id)
                    if product_map:
                        if update or len(new_variants) > 0:
                            product_data["product"]["id"] = product_map.web_product_id
                            url = self.get_products_url(instance_id, f'products/{product_map.web_product_id}.json')
                            response = requests.put(url, headers=headers, data=json.dumps(product_data))
                            _logger.info(f"WSSH Updating Shopify product {product_map.web_product_id}")
                            if response.ok:
                                                                          
                                product_processed = True
                        else:
                            _logger.info(f"WSSH Ignorar, por no update, Shopify product {product_map.web_product_id}")                                                            
                    else:                        
                        product_data["product"]["title"]=f"{product.name} - {template_attribute_value.name}"
                        product_data["product"]["status"]='draft'
                        if product.description:
                            product_data["product"]["body_html"]=product.description

                        url = self.get_products_url(instance_id, 'products.json')
                        response = requests.post(url, headers=headers, data=json.dumps(product_data))
                                                                         

                        if response.ok:
                            product_processed = True
                            shopify_product = response.json().get('product', {})
                            if shopify_product:
                                                                                                  
                                self.env['shopify.product.map'].create({
                                    'web_product_id': shopify_product.get('id'),
                                    'odoo_id': template_attribute_value.id,
                                    'shopify_instance_id': instance_id.id,
                                })

                    if response and response.ok:
                        shopify_product = response.json().get('product', {})
                        if shopify_product:
                            shopify_variants = shopify_product.get('variants', [])
                            self._update_variant_ids(variants, shopify_variants, instance_id)
                    elif response:
                        _logger.error(f"WSSH Error exporting product: {response.text}")
                        raise UserError(f"WSSH Error exporting product {product.name} - {template_attribute_value.name}: {response.text}")

                                                                                                  
                if product_processed:
                    processed_count += 1
                    
                if processed_count >= max_processed:
                    _logger.info("WSSH Processed %d products for instance %s. Stopping export for this run.", processed_count, instance_id.name)
                                                                                                      
                    export_update_time = product.write_date - datetime.timedelta(seconds=1)
                    break                                                                                                      
            
                                                            
            self.write_with_retry(instance_id, 'last_export_product', export_update_time)
            
    def _update_shopify_variant(self, variant, instance_id, headers, option_attr_lines):
        """
        Actualiza los datos de una variante en Shopify (precio, sku, barcode, opciones)
        """
        # Busca si hay color fijo para la variante (split)
        color_value = None
        color_attr_line = next((l for l in option_attr_lines if l.attribute_id.name.lower() == 'color'), None)
        if color_attr_line and instance_id.split_products_by_color:
            color_attr_vals = [v for v in variant.product_template_attribute_value_ids
                               if v.attribute_id.id == color_attr_line.attribute_id.id]
            if color_attr_vals:
                color_value = color_attr_vals[0].product_attribute_value_id
    
        variant_data = self._prepare_shopify_variant_data(
            variant,
            instance_id,
            option_attr_lines,
            color_value=color_value,
            is_update=True
        )
        variant_map = variant.shopify_variant_map_ids.filtered(
            lambda m: m.shopify_instance_id == instance_id
        )
        if not variant_map or not variant_map.web_variant_id:
            _logger.error(f"WSSH No Shopify variant map found for variant {variant.display_name}")
            return
    
                                                                                             
        variant_data["id"] = variant_map.web_variant_id
        url = self.get_products_url(instance_id, f'variants/{variant_map.web_variant_id}.json')
        response = requests.put(url, headers=headers, data=json.dumps({"variant": variant_data}))
        
        if not response.ok:
                                                                                                      
             
            _logger.error(f"WSSH Error updating Shopify variant: {response.text}")
            raise UserError(f"WSSH Error updating variant {variant.display_name}: {response.text}")            
         
    def _export_single_product(self, product, instance_id, headers, update):
        """Exporta un producto sin separación por colores"""
        # --- Determina el mapping posición -> atributo ---
        if product.attribute_line_ids:
            attr_lines = list(product.attribute_line_ids)
            color_line = next((l for l in attr_lines if l.attribute_id.name.lower() == 'color'), None)
            size_line = next((l for l in attr_lines if l.attribute_id.name.lower() in ('size', 'talla')), None)
            other_lines = [l for l in attr_lines if l not in (color_line, size_line)]
            max_options = 3
            pos_map = {}
            if color_line:
                pos_map[instance_id.color_option_position] = color_line
            if size_line:
                pos_map[instance_id.size_option_position] = size_line
            other_pos = 1
            for line in other_lines:
                while other_pos in pos_map:
                    other_pos += 1
                if other_pos > max_options:
                    break
                pos_map[other_pos] = line
                other_pos += 1
            option_attr_lines = [pos_map[pos] for pos in sorted(pos_map)]
        else:
            option_attr_lines = []

        variant_data = [
            self._prepare_shopify_variant_data(
                variant, instance_id, option_attr_lines, is_update=update
            )
            for variant in product.product_variant_ids
            if variant.default_code
        ]

        product_data = {
            "product": {
                "price": product.wholesale_price if not instance_id.prices_include_tax else product.list_price
            }
        }
        
        if not update:
            product_data["product"]["title"] = product.name
            product_data["product"]["body_html"] = product.description or ""
            product_data["product"]["tags"] = ','.join(tag.name for tag in product.product_tag_ids)

        if option_attr_lines and not update:
            options = []
            for idx, attr_line in enumerate(option_attr_lines, 1):
                values = attr_line.value_ids.mapped('name')
                if attr_line.attribute_id.name.lower() in ('size', 'talla'):
                    values = sorted(values, key=get_size_value)
                options.append({
                    "name": attr_line.attribute_id.name,
                    "position": idx,
                    "values": values,
                })
            product_data["product"]["options"] = options

        # Cambio: Usar shopify.product.template.map en lugar de shopify_product_id
        product_map = self.env['shopify.product.template.map'].sudo().search([
            ('odoo_id', '=', product.id),
            ('shopify_instance_id', '=', instance_id.id),
        ], limit=1)

        if product_map and update:
            product_data["product"]["id"] = product_map.web_product_id
            url = self.get_products_url(instance_id, f'products/{product_map.web_product_id}.json')
            response = requests.put(url, headers=headers, data=json.dumps(product_data))
            
            if response.ok:
                for variant in product.product_variant_ids:
                    if variant.default_code:
                        self._update_shopify_variant(variant, instance_id, headers, option_attr_lines)
                    
        else:
            product_data["product"]["status"] = 'draft'
            product_data["product"]["variants"] = variant_data
            url = self.get_products_url(instance_id, 'products.json')
            response = requests.post(url, headers=headers, data=json.dumps(product_data))

        if response.ok:
            shopify_product = response.json().get('product')
            if shopify_product:
                if not product_map:
                    self.env['shopify.product.template.map'].create({
                        'web_product_id': shopify_product.get('id'),
                        'odoo_id': product.id,
                        'shopify_instance_id': instance_id.id,
                    })
                shopify_variants = shopify_product.get('variants', [])
                self._update_variant_ids(product.product_variant_ids, shopify_variants, instance_id)
        else:
            _logger.error(f"WSSH Error exporting product: {response.text}")
            raise UserError(f"WSSH Error exporting product {product.name}: {response.text}")
            
            
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
                     
                
    def _prepare_shopify_variant_data(self, variant, instance_id, option_attr_lines=None, color_value=None, is_update=False):
        """Prepara los datos de la variante para enviar a Shopify"""
        variant_data = {
            "price": variant.product_tmpl_id.wholesale_price if not instance_id.prices_include_tax else variant.list_price,
            "sku": variant.default_code or "",
            "barcode": variant.barcode or "",
            "inventory_management": "shopify"
        }
    
        if is_update:
                                                                             
            variant_map = variant.shopify_variant_map_ids.filtered(
                lambda m: m.shopify_instance_id == instance_id
            )
            if variant_map and variant_map.web_variant_id:
                variant_data["id"] = variant_map.web_variant_id
        else:
            if option_attr_lines:
                value_map = {v.attribute_id.id: v for v in variant.product_template_attribute_value_ids}
                for idx, attr_line in enumerate(option_attr_lines, 1):
                    # Si la opción es Color y hay un valor de color fijo (split), úsalo para todas las variantes del producto split
                    if attr_line.attribute_id.name.lower() == 'color' and color_value is not None:
                
                                                                   
                        variant_data[f"option{idx}"] = color_value.name
                    else:
                        value = value_map.get(attr_line.attribute_id.id)
                        variant_data[f"option{idx}"] = self._extract_name(value.product_attribute_value_id) if value else "Default"
                 
                                                 
                                                  
                                 
                                  
                 
            else:
                                                   
                for idx, attr_val in enumerate(variant.product_template_attribute_value_ids, 1):
                    if idx <= 3:
                        variant_data[f"option{idx}"] = self._extract_name(attr_val.product_attribute_value_id)
    
        return variant_data
        
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
            
            # Si se pasa una selección de productos, se filtran las variantes correspondientes.
            if products:
                # Buscamos las variantes asociadas a los templates seleccionados.
                variants = self.env['product.product'].search([('product_tmpl_id', 'in', products.ids)])
            else:
                # Dominio original para stock.quants, según fecha y último ID exportado.
                #usamos effective_export_date (deberia llamarse (effective write date) que es el mayor entre fecha escritura quant, y fecha creacion mapa stock
                #porque entre la creacion de productos en odoo y la tarea de exportacion de prodcutos que crea el mapa de stock se ha podido colar la tarea de exportacion de stock,
                #retrasando la ultima fecha de exportacion de stock saltandose los quant modificados
                quant_domain = [
                    ('effective_export_date', '>=', shopify_instance.last_export_stock or '1900-01-01 00:00:00'),
                    ('id', '>', shopify_instance.last_export_stock_id or 0)
                ]
                if location.import_stock_warehouse_id:
                    quant_domain.append(('location_id', '=', location.import_stock_warehouse_id.id))
                
                # Buscar stock.quants con el orden apropiado
                order = "effective_export_date asc, id asc"
                if shopify_instance.last_export_stock_id > 0:
                    order = "id asc"
                    _logger.info("WSSH Continuando desde ID %s (timeout previo)", shopify_instance.last_export_stock_id)
                else:
                    _logger.info("WSSH Iniciando nuevo proceso desde fecha %s", shopify_instance.last_export_stock)
                
                quants = self.env['stock.quant'].sudo().search(quant_domain, order=order)
                # Obtener variantes únicas de los quants        
                variants = self.env['product.product'].sudo().browse(quants.mapped('product_id').ids)
            
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
                
                # Obtener cantidad disponible desde stock.quant
                domain = [('product_id', '=', variant.id)]
                if location.import_stock_warehouse_id:
                    domain.append(('location_id', '=', location.import_stock_warehouse_id.id))
                quant = self.env['stock.quant'].sudo().search(domain, limit=1)
                available_qty = quant.quantity if quant else 0
                
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
                    # Actualizar shopify_instance tras cada éxito con el ID del quant
                    if quant:
                        self.write_with_retry(shopify_instance, 'last_export_stock_id', quant.id)
                else:
                    _logger.warning("WSSH Failed to update stock for product %s (variant %s): %s en instancia %s",
                                    variant.product_tmpl_id.name, variant.name, response.text, shopify_instance.name)
                
                # Verificar timeout
                if time.time() - iteration_start_time > iteration_timeout:
                    _logger.error("WSSH Timeout de iteración alcanzado para el producto %s en instancia %s",
                                  variant.default_code, shopify_instance.name)
                    return updated_ids
            
            # Si se procesan todos los productos, actualizar a la fecha actual
            _logger.info("WSSH Update stock final completo para %s", shopify_instance.name)
            self.write_with_retry(shopify_instance, 'last_export_stock', fields.Datetime.now())
            self.write_with_retry(shopify_instance, 'last_export_stock_id', 0)
            return updated_ids

    # Dentro de la clase product.template heredada (ws_shopify.models.product)

    def _check_last_shopify_product_map(self, shopify_instance):
        """
        Verifica si el último producto *relevante* (con variantes y barcode)
        creado en Shopify por el conector tiene un mapeo correspondiente en Odoo.
        Se considera relevante si tiene variantes y al menos una con código de barras.
        Utiliza la librería requests como en otros métodos.
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
                # ¡Problema detectado! El último producto *relevante* (con barcode) en Shopify no está mapeado en Odoo
                error_msg = (
                    f"WSSH ¡ALERTA CRÍTICA! Se encontró el último producto *relevante* (con barcode) creado en Shopify "
                    f"(ID: {relevant_shopify_product_id}, Instancia: {shopify_instance.name}) PERO NO TIENE "
                    f"UN REGISTRO DE MAPEO CORRESPONDIENTE EN ODOO ('shopify.product.template.map' ni 'shopify.product.map'). "
                    "Esto indica una inconsistencia previa que podría llevar a duplicados de productos *con barcode*. "
                    "Se aborta la exportación de productos para esta instancia hasta corregir el mapeo."
                )
                _logger.error(error_msg)
                # Abortar la ejecución
                raise UserError(_(error_msg))

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

     def _export_single_product_v2(self, product, instance_id, headers, update):
        """
        Exporta o actualiza un producto 'single' en Shopify usando GraphQL.
        - Si el producto NO existe en Shopify: lo crea siempre.
        - Si el producto YA existe y update=True: actualiza SOLO precios de variantes.
        - Si el producto YA existe y update=False: no hace nada.
        En creación, actualiza SKU/barcode/price SOLO para la primera variante autogenerada (REST), y crea el resto en bulk.
        """
        _logger.info("WSSH Dentro Exporta no split v2")
        option_attr_lines = self._get_option_attr_lines(product, instance_id)
        _logger.info("WSSH Single p1")
        
        product_map = product.shopify_product_map_ids.filtered(lambda m: m.shopify_instance_id.id == instance_id.id)
        shopify_product_exists = bool(product_map and product_map.web_product_id)
        need_create = not shopify_product_exists
        need_update = (shopify_product_exists and update)
        
        # Construcción del payload para la llamada GraphQL
        product_input = self._build_graphql_product_input_v2(product, instance_id, option_attr_lines, update=need_update)
        _logger.info("WSSH product_input (GraphQL): %s", json.dumps(product_input, indent=2, default=str))
        _logger.info("WSSH Single p2")
        graphql_response = self._shopify_graphql_call_v2(instance_id, product_input, update=need_update)
        _logger.info("WSSH graphql_response: %s", json.dumps(graphql_response, indent=2, default=str))
        _logger.info("WSSH Single p3")
        product_id, variant_gids = self._handle_graphql_product_response_v2(
            product, instance_id, graphql_response, update=need_update
        )
        _logger.info("WSSH Single p4")
    
        if not product_id:
            _logger.error("WSSH No se obtuvo product_id tras la creación/update del producto. Abortando exportación.")
            return
    
        variant_inputs = [
            self._prepare_shopify_single_product_variant_bulk_data(v, instance_id, option_attr_lines)
            for v in product.product_variant_ids if v.default_code
        ]
    
        combo_to_gid = self._get_shopify_variant_combo_map(product, variant_gids, option_attr_lines, graphql_response)
        first_combo = tuple(line.value_ids[0].name for line in option_attr_lines) if option_attr_lines else ()
    
        if need_create:
            # CREACIÓN: Actualiza la primera variante (solo price, sku, barcode) por REST.
            for v, vinp in zip(product.product_variant_ids, variant_inputs):
                combo = tuple(opt['name'] for opt in vinp['optionValues'])
                gid = combo_to_gid.get(combo)
                if combo == first_combo and gid:
                    self._shopify_update_first_variant_rest(
                        instance_id,
                        gid,
                        v.default_code or "",
                        v.barcode or "",
                        vinp.get("price", "")
                    )
            # Crea el resto de variantes (las que no sean la primera)
            create_variants = []
            for v, vinp in zip(product.product_variant_ids, variant_inputs):
                combo = tuple(opt['name'] for opt in vinp['optionValues'])
                if combo != first_combo:
                    create_variants.append(vinp)
            if create_variants:
                _logger.info("WSSH Bulk create variantes: %s", json.dumps(create_variants, indent=2, default=str))
                bulk_response = self._shopify_graphql_variants_bulk_create(instance_id, product_id, create_variants)
                _logger.info("WSSH Bulk GraphQL response: %s", json.dumps(bulk_response, indent=2, default=str))
                self._handle_graphql_variant_bulk_response(product, instance_id, bulk_response)
        elif need_update:
            # UPDATE: actualiza SOLO precios de todas las variantes
            updates_bulk = []
            for v, vinp in zip(product.product_variant_ids, variant_inputs):
                combo = tuple(opt['name'] for opt in vinp['optionValues'])
                gid = combo_to_gid.get(combo)
                if gid:
                    updates_bulk.append({
                        "id": gid,
                        "price": vinp.get("price", "")
                    })
            if updates_bulk:
                _logger.info("WSSH BulkUpdate precios variants: %s", json.dumps(updates_bulk, indent=2, default=str))
                self._shopify_graphql_variants_bulk_update(instance_id, product_id, updates_bulk)
        else:
            _logger.info("WSSH Producto ya existe y update=False: no se hace ninguna actualización de variantes.")


    def _build_graphql_product_input_v2(self, product, instance_id, option_attr_lines, update):
        """Construye payload GraphQL para crear el producto con productOptions."""
        product_options = []
        for line in option_attr_lines:
            values = line.value_ids.mapped('name')
            values = sorted(values, key=get_size_value) if line.attribute_id.name.lower() in ('size', 'talla') else sorted(values)
            value_dicts = [{"name": v} for v in values]
            product_options.append({
                "name": line.attribute_id.name,
                "values": value_dicts,
            })
        product_input = {
            "title": product.name,
            "descriptionHtml": product.description or "",
            "tags": ','.join(tag.name for tag in product.product_tag_ids),
            "status": "DRAFT",
            "productOptions": product_options
        }
        if update:
            product_map = self.env['shopify.product.template.map'].sudo().search([
                ('odoo_id', '=', product.id),
                ('shopify_instance_id', '=', instance_id.id),
            ], limit=1)
            if product_map:
                shopify_id = product_map.web_product_id
                if not str(shopify_id).startswith("gid://"):
                    shopify_id = f"gid://shopify/Product/{shopify_id}"
                product_input["id"] = shopify_id
        return product_input

    def _shopify_graphql_call_v2(self, instance_id, product_input, update):
        """Ejecuta llamada GraphQL a Shopify para crear/actualizar producto (sin variantes)."""
        graphql_url = f"https://{instance_id.shopify_host}.myshopify.com/admin/api/{instance_id.shopify_version}/graphql.json"
        headers = {
            "X-Shopify-Access-Token": instance_id.shopify_shared_secret,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        mutation = """
        mutation product%s($input: ProductInput!) {
            product%s(input: $input) {
                product { 
                    id
                    variants(first: 1) {
                        edges {
                            node {
                                id
                                selectedOptions { name value }
                            }
                        }
                    }
                }
                userErrors { field message }
            }
        }
        """ % ("Update" if update else "Create", "Update" if update else "Create")
        _logger.info("WSSH DEBUG GraphQL mutation: %s", mutation)
        _logger.info("WSSH DEBUG GraphQL endpoint: %s", graphql_url)
        _logger.info("WSSH DEBUG GraphQL headers: %s", headers)
        _logger.info("WSSH DEBUG GraphQL variables: %s", json.dumps({"input": product_input}, indent=2, default=str))
        response = requests.post(graphql_url, headers=headers, json={
            "query": mutation,
            "variables": {"input": product_input}
        })
        _logger.info("WSSH DEBUG Raw GraphQL HTTP status: %s", response.status_code)
        _logger.info("WSSH DEBUG Raw GraphQL response text: %s", response.text)
        try:
            return response.json()
        except Exception as ex:
            _logger.error("WSSH ERROR al decodificar JSON de respuesta GraphQL: %s", ex)
            raise UserError("WSSH ERROR: respuesta no JSON de Shopify: %s" % response.text)

    def _handle_graphql_product_response_v2(self, product, instance_id, response_json, update):
        """
        Procesa la respuesta GraphQL de creación/actualización de producto.
        Devuelve GID de producto y lista ordenada de GIDs de variantes.
        """
        operation = "productUpdate" if update else "productCreate"
        _logger.info("WSSH DEBUG parsed response_json: %s", json.dumps(response_json, indent=2, default=str))
        data = response_json.get("data", {}).get(operation, {})
        errors = data.get("userErrors", [])
        product_data = data.get("product")
        variant_gids = []
        if product_data and product_data.get("variants", {}).get("edges"):
            # Obtén todos los GIDs en orden
            variant_gids = [
                {
                    "id": edge["node"]["id"],
                    "selectedOptions": edge["node"]["selectedOptions"]
                }
                for edge in product_data["variants"]["edges"]
            ]
        if errors:
            _logger.error(f"WSSH Error {operation}: {errors}")
            raise UserError(f"WSSH Error exporting product {product.name}: {errors}")
        if product_data and product_data.get("id"):
            shopify_product_gid = product_data["id"]
            shopify_product_id = shopify_product_gid.split("/")[-1]
            product_map = self.env['shopify.product.template.map'].sudo().search([
                ('odoo_id', '=', product.id),
                ('shopify_instance_id', '=', instance_id.id),
            ], limit=1)
            if not product_map:
                self.env['shopify.product.template.map'].create({
                    'web_product_id': shopify_product_id,
                    'odoo_id': product.id,
                    'shopify_instance_id': instance_id.id,
                })
            return shopify_product_gid, variant_gids
        return None, []
        
    def _prepare_shopify_single_product_variant_bulk_data(self, variant, instance_id, option_attr_lines):
        """Prepara cada variante para bulk create GraphQL usando optionName."""
        value_map = {v.attribute_id.id: v for v in variant.product_template_attribute_value_ids}
        option_values = []
        for line in option_attr_lines:
            value = value_map.get(line.attribute_id.id)
            value_name = self._extract_name(value.product_attribute_value_id) if value else "Default"
            option_values.append({
                "optionName": line.attribute_id.name,
                "name": value_name,
                                      
            })
        result = {
            "price": str(variant.product_tmpl_id.wholesale_price if not instance_id.prices_include_tax else variant.list_price),
            "barcode": variant.barcode or "",
            "optionValues": option_values
                              
                                             
        }
        if variant.default_code:
            result["inventoryItem"] = {"sku": variant.default_code}
        return result           
        
    def _shopify_graphql_variants_bulk_create(self, instance_id, product_gid, variant_inputs):
        """Llama a productVariantsBulkCreate en Shopify para bulk create de variantes."""
        graphql_url = f"https://{instance_id.shopify_host}.myshopify.com/admin/api/{instance_id.shopify_version}/graphql.json"
        headers = {
            "X-Shopify-Access-Token": instance_id.shopify_shared_secret,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        mutation = """
        mutation productVariantsBulkCreate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
            productVariantsBulkCreate(productId: $productId, variants: $variants) {
                productVariants {
                    id
                }
                userErrors {
                    field
                    message
                }
            }
        }
        """
        variables = {
            "productId": product_gid,
            "variants": variant_inputs
        }
        _logger.info("WSSH Bulk GraphQL mutation: %s", mutation)
        _logger.info("WSSH Bulk GraphQL endpoint: %s", graphql_url)
        _logger.info("WSSH Bulk GraphQL variables: %s", json.dumps(variables, indent=2, default=str))
        response = requests.post(graphql_url, headers=headers, json={
            "query": mutation,
            "variables": variables
        })
        _logger.info("WSSH Bulk GraphQL HTTP status: %s", response.status_code)
        _logger.info("WSSH Bulk GraphQL response text: %s", response.text)
        try:
            return response.json()
        except Exception as ex:
            _logger.error("WSSH ERROR al decodificar JSON de respuesta GraphQL (bulk): %s", ex)
            raise UserError("WSSH ERROR: respuesta no JSON de Shopify (bulk): %s" % response.text)

    def _shopify_graphql_variants_bulk_update(self, instance_id, product_gid, variant_updates):
        """Llama a productVariantsBulkUpdate en Shopify para actualizar variantes."""
        graphql_url = f"https://{instance_id.shopify_host}.myshopify.com/admin/api/{instance_id.shopify_version}/graphql.json"
        headers = {
            "X-Shopify-Access-Token": instance_id.shopify_shared_secret,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        mutation = """
        mutation productVariantsBulkUpdate($productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
          productVariantsBulkUpdate(productId: $productId, variants: $variants) {
            product {
              id
            }
            productVariants {
              id
              price
              sku
              barcode
            }
            userErrors {
              field
              message
            }
          }
        }
        """
        variables = {
            "productId": product_gid,
            "variants": variant_updates
        }
        _logger.info("WSSH BulkUpdate GraphQL mutation: %s", mutation)
        _logger.info("WSSH BulkUpdate GraphQL endpoint: %s", graphql_url)
        _logger.info("WSSH BulkUpdate GraphQL variables: %s", json.dumps(variables, indent=2, default=str))
        response = requests.post(graphql_url, headers=headers, json={
            "query": mutation,
            "variables": variables
        })
        _logger.info("WSSH BulkUpdate GraphQL HTTP status: %s", response.status_code)
        _logger.info("WSSH BulkUpdate GraphQL response text: %s", response.text)
        try:
            return response.json()
        except Exception as ex:
            _logger.error("WSSH ERROR al decodificar JSON de respuesta GraphQL (bulk update): %s", ex)
            raise UserError("WSSH ERROR: respuesta no JSON de Shopify (bulk update): %s" % response.text)

    def _handle_graphql_variant_bulk_response(self, product, instance_id, response_json):
        """
        Procesa la respuesta de productVariantsBulkCreate para guardar los GIDs de las variantes de Shopify.
        """
        if not response_json:
            _logger.error("WSSH: No response_json en bulk variant response")
            return
        user_errors = response_json.get("data", {}).get("productVariantsBulkCreate", {}).get("userErrors", [])
        if user_errors:
            _logger.error("WSSH Bulk create variants userErrors: %s", user_errors)
        else:
            _logger.info("WSSH Bulk create variants sin errores")
        # Extensión: guarda mapping Odoo<->Shopify si es necesario
            
        
    def _get_shopify_variant_combo_map(self, product, variant_gids, option_attr_lines, graphql_response):
        """
        Mapea cada combinación de opciones (tuple) con su GID de Shopify.
        Esto permite identificar la primera variante y todas las demás para update/create.
        """
        combo_to_gid = {}
        for v in variant_gids:
            combo = tuple(opt['value'] for opt in v["selectedOptions"])
            combo_to_gid[combo] = v["id"]
        return combo_to_gid
        
    def _get_option_attr_lines(self, product, instance_id):
        """
        Obtiene la lista de líneas de atributos (option_attr_lines) en el orden adecuado
        para construir el input de opciones para GraphQL.
        """
        attr_lines = list(product.attribute_line_ids)
        color_line = next((l for l in attr_lines if l.attribute_id.name.lower() == 'color'), None)
        size_line = next((l for l in attr_lines if l.attribute_id.name.lower() in ('size', 'talla')), None)
        other_lines = [l for l in attr_lines if l not in (color_line, size_line)]
        pos_map = {}
        max_options = 3
        if color_line:
            pos_map[1] = color_line
        if size_line:
            pos_map[2] = size_line
        other_pos = 3
        for line in other_lines:
            while other_pos in pos_map:
                other_pos += 1
            if other_pos > max_options:
                break
            pos_map[other_pos] = line
            other_pos += 1
        return [pos_map[pos] for pos in sorted(pos_map)]        
        
    def _shopify_update_first_variant_rest(self, instance_id, variant_gid, sku, barcode, price):
        """Actualiza la primera variante (SKU/barcode/price) por REST, ya que GraphQL no lo soporta."""
        variant_id = self._gid_to_id(variant_gid)
        url = f"https://{instance_id.shopify_host}.myshopify.com/admin/api/{instance_id.shopify_version}/variants/{variant_id}.json"
        headers = {
            "X-Shopify-Access-Token": instance_id.shopify_shared_secret,
            "Content-Type": "application/json"
        }
        data = {
            "variant": {
                "id": int(variant_id),
                "sku": sku,
                "barcode": barcode,
                "price": price
            }
        }
        _logger.info("WSSH Actualizando primera variante por REST: %s", json.dumps(data))
        response = requests.put(url, headers=headers, data=json.dumps(data))
        if not response.ok:
            _logger.error(f"WSSH Error actualizando primera variante REST: {response.text}")
            raise UserError(f"Error actualizando primera variante REST: {response.text}")
        _logger.info("WSSH Actualización primera variante OK por REST: %s", response.text)
        return response.json()   
    
    def _gid_to_id(gid):
        # Espera una cadena tipo "gid://shopify/ProductVariant/51258548519258"
        return gid.split('/')[-1]

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
    
    