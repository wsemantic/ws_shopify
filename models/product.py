# inherit class product.template and add fields for shopify instance and shopify product id
import base64
import json

import requests
from bs4 import BeautifulSoup
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import config
config['limit_time_real'] = 10000000
import logging

_logger = logging.getLogger(__name__)

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

    shopify_variant_map_ids = fields.One2many(
        "shopify.variant.map",
        "odoo_id",
        string="Shopify Variant Mappings",
        help="Mappings to Shopify variants across multiple websites"
    )


class ProductTemplate(models.Model):
    _inherit = 'product.template'


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
          
          # Si no existe, buscar por las variantes (shopify_variant_id o default_code)
          for variant in shopify_product.get('variants', []):
              shopify_variant_id = variant.get('id')
              sku = variant.get('sku')
              _logger.info(f"WSSH iterando variant {sku}")
              # Buscar por shopify_variant_id o default_code (SKU)
              existing_variant = self.env['product.product'].sudo().search([
                  '|',
                  ('shopify_variant_map_ids.web_variant_id', '=', shopify_variant_id),
                  ('default_code', '=', sku),
              ], limit=1)
              
              if existing_variant:
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
                  _logger.info("WSSH No matching product found for Shopify Variant ID: %s or SKU: %s", shopify_variant_id, sku)
          else:
              # Si no se encuentra el producto ni sus variantes, crear el producto en Odoo
              if not skip_existing_products:
                  _logger.info(f"WSSH Creando producto ")
                  #product_template = self._create_product_from_shopify(shopify_product, shopify_instance_id)
                  #if product_template:
                  #    product_list.append(product_template.id)
      
      return product_list

    def _create_product_from_shopify(self, shopify_product, shopify_instance_id):
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
        
        # Asignar el shopify_product_id a las líneas de atributos
        for attribute_line in product_template.attribute_line_ids:
            for attribute_value in attribute_line.product_template_value_ids:
                if attribute_value.attribute_id.name.lower() == 'color':
                    attribute_value.write({
                        'shopify_product_id': shopify_product.get('id'),
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



                        # if level.get('available') != None:
                        #     res_product_qty = self.env['stock.change.product.qty'].sudo().search(
                        #         [('product_id', '=', product.id)], limit=1)

                        # dict_q = {}
                        # dict_q['new_quantity'] = level.get('available')
                        # dict_q['product_id'] = product.id
                        # dict_q['product_tmpl_id'] = product.product_tmpl_id.id
                        #
                        # if not res_product_qty:
                        #     create_qty = self.env['stock.change.product.qty'].sudo().create(dict_q)
                        #     create_qty.change_product_qty()
                        # else:
                        #     write_qty = res_product_qty.sudo().write(dict_q)
                        #     qty_id = self.env['stock.change.product.qty'].sudo().search(
                        #         [('product_id', '=', product.id)], limit=1)
                        #     if qty_id:
                        #         qty_id.change_product_qty()


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

    def export_products_to_shopify(self, shopify_instance_ids, update=False):
        """
        Exporta productos a Shopify, filtrando por aquellos modificados desde la última exportación.
        """
        color_attribute = None
        for attr in self.env['product.attribute'].search([]):
            if attr.name and attr.name.lower().find('color') != -1:
                color_attribute = attr
                break

        for instance_id in shopify_instance_ids:                                                                             
            if instance_id.last_export_product:
                _logger.info(f"WSSH Starting product export por fecha {instance_id.last_export_product} instance {instance_id.name} atcolor {color_attribute}") 
                domain = [('write_date', '>', instance_id.last_export_product)]
            else:
                _logger.info("WSSH Starting product export SIN fecha for instance %s", instance_id.name)
                domain = []

            products_to_export = self.search(domain, order='create_date',limit=1)
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
            max_processed = 10  # Limitar a 10 productos exportados por ejecución
        
            for product in products_to_export:                
                if not instance_id.split_products_by_color:
                    raise UserError(f"WSSH De momento solo soportado split por color")
                    self._export_single_product(product, instance_id, headers, update)
                    continue

                color_line = product.attribute_line_ids.filtered(
                    lambda l: l.attribute_id.name.lower() == 'color')
                if not color_line:
                    raise UserError(f"WSSH Producto sin color {product.name}, de momento solo soportado split por color")
                    self._export_single_product(product, instance_id, headers, update)
                    continue
               
                for template_attribute_value in color_line.product_template_value_ids:
                    _logger.info(f"WSSH Exporting product: {product.name} (ID:{product.id}) update {update} variante {template_attribute_value.name}")
                    response = None
                    # Filtrar variantes para este color
                    variants = product.product_variant_ids.filtered(
                        lambda v: template_attribute_value in v.product_template_attribute_value_ids and v.barcode
                    )
                    if not variants:
                        _logger.info(f"WSSH No hay variantes con codigo {template_attribute_value.name}")
                        continue

                    # Verificar si hay nuevas variantes sin mapeo
                    new_variants = variants.filtered(
                        lambda v: not v.shopify_variant_map_ids.filtered(lambda m: m.shopify_instance_id == instance_id)
                    )
                    _logger.info(f"WSSH Total variants: {len(variants)}, New variants: {len(new_variants)}")
                    # Preparar datos para Shopify
                    variant_data = [
                        self._prepare_shopify_variant_data(variant, instance_id, template_attribute_value, True, update)
                        for variant in variants
                        if variant.default_code
                    ]
                    
                    # Si no hay variantes con default_code, se salta este producto virtual
                    if not variant_data:
                        _logger.info("WSSH Skipping Shopify export for product '%s' with color '%s' because no variant has default_code",
                                     product.name, template_attribute_value.name)
                        continue

                    product_data = {
                        "product": {
                            "title": f"{product.name} - {template_attribute_value.name}",
                            "body_html": product.description or "",
                            "options": [
                                {
                                    "name": "Color",
                                    "position": instance_id.color_option_position,
                                    "values": sorted(set(v.get(f"option{instance_id.color_option_position}", "") for v in variant_data))
                                },
                                {
                                    "name": "Size",
                                    "position": instance_id.size_option_position,
                                    "values": sorted(set(v.get(f"option{instance_id.size_option_position}", "") for v in variant_data))
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
                                # Actualizar las variantes individualmente
                                processed_count += 1
                        else:
                            _logger.info(f"WSSH Ignorar, por no update, Shopify product {product_map.web_product_id}")                                                            
                    else:                        
                        product_data["product"]["status"]='draft'
                        url = self.get_products_url(instance_id, 'products.json')
                        response = requests.post(url, headers=headers, data=json.dumps(product_data))
                        _logger.info("WSSHCreating new Shopify product")

                        if response.ok:
                            processed_count += 1
                            shopify_product = response.json().get('product', {})
                            if shopify_product:
                                # Guardar el ID del producto y actualizar los IDs de las variantes
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

                if processed_count >= max_processed:
                    _logger.info("WSSH Processed %d products for instance %s. Stopping export for this run.", processed_count, instance_id.name)
                    break
                
            # Actualizar la fecha de la última exportación
            instance_id.last_export_product = fields.Datetime.now()
            
    def _update_shopify_variant(self, variant, instance_id, headers):
        if not variant.barcode:
            _logger.warning(f"WSSH Skipping variant {variant.default_code} without barcode")
            return None  # O lanzar una excepción si prefieres
        
        variant_map = variant.shopify_variant_map_ids.filtered(lambda m: m.shopify_instance_id == instance_id)
        if not variant_map or not variant_map.web_variant_id:
            _logger.warning("No se encontró mapping para la variante %s en la instancia %s", variant.id, instance_id.name)
            return
            
        """Actualiza una variante en Shopify usando el endpoint variants/<id_variant>.json"""
        variant_data = self._prepare_shopify_variant_data(variant, instance_id, is_update=True)
        url = self.get_products_url(instance_id, f'variants/{variant_map.web_variant_id}.json')
        response = requests.put(url, headers=headers, data=json.dumps({"variant": variant_data}))
        
        if response.ok:
            _logger.info(f"WSSH Successfully updated variant {variant.default_code} in Shopify")
        else:
            _logger.error(f"WSSH Error updating variant {variant.default_code}: {response.text}")
            raise UserError(f"WSSH Error updating variant {variant.default_code}: {response.text}")            
         
    def _export_single_product(self, product, instance_id, headers, update):
        """Exporta un producto sin separación por colores"""
        variant_data = [
            self._prepare_shopify_variant_data(variant, instance_id, is_update=update)
            for variant in product.product_variant_ids
            if variant.default_code
        ]

        product_data = {
            "product": {
                "title": product.name,
                "body_html": product.description or "",
                "tags": ','.join(tag.name for tag in product.product_tag_ids)
            }
        }

        # Añadir opciones si hay atributos
        if product.attribute_line_ids:
            options = []
            for idx, attr_line in enumerate(product.attribute_line_ids, 1):
                if idx <= 3:
                    options.append({
                        "name": attr_line.attribute_id.name,
                        "position": idx,
                        "values": attr_line.value_ids.mapped('name')
                    })
            product_data["product"]["options"] = options

        # Si el producto ya existe, solo actualizamos el producto y sus opciones
        if product.shopify_product_id and update:
            product_data["product"]["id"] = product.shopify_product_id
            url = self.get_products_url(instance_id, f'products/{product.shopify_product_id}.json')
            response = requests.put(url, headers=headers, data=json.dumps(product_data))
            
            if response.ok:
                # Actualizar las variantes individualmente
                for variant in product.product_variant_ids:
                    if variant.default_code:
                        self._update_shopify_variant(variant, instance_id, headers)
                    
        else:
            # Si es un nuevo producto, enviamos también las variantes
            product_data["product"]["status"]='draft'
            product_data["product"]["variants"] = variant_data
            url = self.get_products_url(instance_id, 'products.json')
            response = requests.post(url, headers=headers, data=json.dumps(product_data))

        if response.ok:
            shopify_product = response.json().get('product')
            if shopify_product:
                # Actualizar ID del producto y de sus variantes
                product.shopify_product_id = shopify_product.get('id')
                shopify_variants = shopify_product.get('variants', [])
                self._update_variant_ids(product.product_variant_ids, shopify_variants,instance_id)

                product.is_shopify_product = True
                product.shopify_instance_id = instance_id.id
                product.is_exported = True
                _logger.info(f"WSSH Successfully exported product {product.name}")
        else:
            _logger.error(f"WSSH Error exporting product: {response.text}")
            raise UserError(f"WSSH Error exporting product {product.name}: {response.text}")
            
    def _update_variant_ids(self, odoo_variants, shopify_variants, instance_id):
        """
        Actualiza los IDs de las variantes de Shopify en las variantes de Odoo, 
        y actualiza los mappings de stock basados en el stock.quant correspondiente.
        """
        shopify_location = self.env['shopify.location'].sudo().search([
            ('shopify_instance_id', '=', instance_id.id)
        ], limit=1)
        
        for odoo_variant in odoo_variants:
            matched_shopi_variant = None
            # Buscar coincidencia en shopify_variants usando SKU o barcode
            for shopify_variant in shopify_variants:
                if shopify_variant.get('sku') in (odoo_variant.default_code, odoo_variant.barcode):
                    matched_shopi_variant = shopify_variant
                    break
    
            if matched_shopi_variant:
                # Actualizar o crear el mapeo de la variante
                variant_map = odoo_variant.shopify_variant_map_ids.filtered(
                    lambda m: m.shopify_instance_id == instance_id
                )
                if variant_map:
                    if variant_map.web_variant_id != matched_shopi_variant.get('id'):
                        variant_map.write({'web_variant_id': matched_shopi_variant.get('id')})
                        _logger.info("Updated variant map for Odoo variant (SKU: %s)", odoo_variant.default_code)
                else:
                    self.env['shopify.variant.map'].create({
                        'web_variant_id': matched_shopi_variant.get('id'),
                        'odoo_id': odoo_variant.id,
                        'shopify_instance_id': instance_id.id,
                    })
                    _logger.info("Created variant map for Odoo variant (SKU: %s)", odoo_variant.default_code)
    
                # Actualizar el mapeo de stock en stock.quant
                if shopify_location:
                    domain = [('product_id', '=', odoo_variant.id)]
                    if shopify_location.import_stock_warehouse_id:
                        domain.append(('location_id', '=', shopify_location.import_stock_warehouse_id.id))
                    
                    stock_quant = self.env['stock.quant'].sudo().search(domain, limit=1)
                    if stock_quant:
                        stock_map = stock_quant.shopify_stock_map_ids.filtered(
                            lambda m: m.shopify_instance_id == instance_id
                        )
                        if stock_map:
                            if stock_map.web_stock_id != matched_shopi_variant.get('inventory_item_id'):
                                stock_map.write({'web_stock_id': matched_shopi_variant.get('inventory_item_id')})
                                _logger.info("Updated stock map for Odoo variant (SKU: %s)", odoo_variant.default_code)
                        else:
                            self.env['shopify.stock.map'].create({
                                'web_stock_id': matched_shopi_variant.get('inventory_item_id'),
                                'odoo_id': stock_quant.id,
                                'shopify_instance_id': instance_id.id,
                            })
                            _logger.info("Created stock map for Odoo variant (SKU: %s)", odoo_variant.default_code)
                    else:
                        _logger.warning("No stock.quant found for Odoo variant (SKU: %s) in location %s",
                                        odoo_variant.default_code, shopify_location.name)
                else:
                    _logger.warning("No shopify.location found for instance %s", instance_id.name)
            else:
                sku = odoo_variant.default_code or 'N/A'
                _logger.warning("No matching Shopify variant found for Odoo variant with SKU %s", sku)   
                     
                
    def _prepare_shopify_variant_data(self, variant, instance_id, template_attribute_value=None, is_color_split=False, is_update=False):
        """Prepara los datos de la variante para enviar a Shopify"""
        variant_data = {
            "price": variant.lst_price,
            "sku": variant.default_code or "",
            "barcode": variant.barcode or "",
            "inventory_management": "shopify"
        }

        if is_update:
            # Obtener el mapping de la variante para la instancia específica
            variant_map = variant.shopify_variant_map_ids.filtered(lambda m: m.shopify_instance_id == instance_id)
            if variant_map and variant_map.web_variant_id:
                variant_data["id"] = variant_map.web_variant_id

        if is_color_split and template_attribute_value:
            # Si estamos separando por colores, solo usamos el atributo talla
            size_option_key = f"option{instance_id.size_option_position}"
            color_option_key = f"option{instance_id.color_option_position}"
            
            variant_data[color_option_key] = template_attribute_value.name if is_color_split and template_attribute_value else ""
            size_value = variant.product_template_attribute_value_ids.filtered(lambda v: v.attribute_id.name.lower() != 'color')
            variant_data[size_option_key] = size_value.name if size_value else "Default"
        else:
            # Caso normal - todos los atributos
            for idx, attr_val in enumerate(variant.product_template_attribute_value_ids, 1):
                if idx <= 3:  # Shopify solo permite 3 opciones
                    variant_data[f"option{idx}"] = attr_val.name

        return variant_data
        
                 
    def export_stock_to_shopify(self, shopify_instance):
        _logger.info("WSSH Exportar stocks")
        updated_ids = []
        location = self.env['shopify.location'].sudo().search([('shopify_instance_id', '=', shopify_instance.id)], limit=1)
        
        if not location:
            _logger.warning("No shopify.location found for instance %s", shopify_instance.name)
            return updated_ids
    
        domain = [
            ('shopify_stock_map_ids.shopify_instance_id', '=', shopify_instance.id),
            ('shopify_stock_map_ids.web_stock_id', '!=', False)
        ]
        if location and location.import_stock_warehouse_id:
            domain.append(('location_id', '=', location.import_stock_warehouse_id.id))
            
        if shopify_instance.last_export_stock:
            domain.append(('write_date', '>', shopify_instance.last_export_stock))
        
        stock_quants = self.env['stock.quant'].sudo().search(domain, order="write_date asc")
        _logger.info(f"WSSH Found {len(stock_quants)} quants desde {shopify_instance.last_export_stock}")
        
        product_data = {}
        for quant in stock_quants:
            product = quant.product_id
            stock_map = quant.shopify_stock_map_ids.filtered(lambda m: m.shopify_instance_id == shopify_instance)
            if not stock_map or not stock_map.web_stock_id:
                continue
            if product not in product_data:
                product_data[product] = {'quantity': 0, 'write_date': quant.write_date, 'inventory_item_id': stock_map.web_stock_id}
            product_data[product]['quantity'] += quant.quantity
            if quant.write_date > product_data[product]['write_date']:
                product_data[product]['write_date'] = quant.write_date
        
        sorted_products = sorted(product_data.items(), key=lambda x: x[1]['write_date'])
        
        # Variables para controlar el tiempo entre peticiones y el tiempo total de iteración        
        last_query_time = 0.0
        iteration_timeout = 500
        iteration_start_time = time.time()
        
        for product, data in sorted_products:
            available_qty = data['quantity']
            current_write_date = data['write_date']
            inventory_item_id = data['inventory_item_id']
            
            elapsed = time.time() - last_query_time
            if elapsed < 0.5:
                time.sleep(0.5 - elapsed)
            last_query_time = time.time()
            
            url = self.get_products_url(shopify_instance, 'inventory_levels/set.json')
            headers = {
                "X-Shopify-Access-Token": shopify_instance.shopify_shared_secret,
                "Content-Type": "application/json"
            }
            data_payload = {
                "location_id": location.shopify_location_id,
                "inventory_item_id": inventory_item_id,
                "available": int(available_qty),
            }
            
            response = requests.post(url, headers=headers, json=data_payload)
            if response.status_code in (200, 201):
                _logger.info("WSSH Stock updated for product %s (variant %s): %s available",
                             product.product_tmpl_id.name, product.name, available_qty)
                updated_ids.append(product.id)
            else:
                _logger.warning("WSSH Failed to update stock for product %s (variant %s): %s",
                                product.product_tmpl_id.name, product.name, response.text)
            
            if time.time() - iteration_start_time > iteration_timeout:
                adjusted_write_date = current_write_date - timedelta(seconds=1)
                _logger.error("WSSH Timeout de iteración alcanzado para el producto %s. Actualizando last_export_stock con write_date %s",
                              product.default_code, adjusted_write_date)
                shopify_instance.last_export_stock = adjusted_write_date
                return updated_ids
        
        shopify_instance.last_export_stock = fields.Datetime.now()
        return updated_ids

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
