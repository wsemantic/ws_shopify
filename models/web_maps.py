# -*- coding: utf-8 -*-
from odoo import models, fields, api

class ShopifyProductMap(models.Model): #TODO refactorizar a ShopifyProductColorValueMap
    _name = 'shopify.product.map'
    _description = 'Mapping del producto entre la web y Odoo'

    web_product_id = fields.Char(string="ID del producto en la web", required=True)
    odoo_id = fields.Many2one('product.template.attribute.value', string="Producto en Odoo", required=True)
    shopify_instance_id = fields.Many2one('shopify.web', string='Shopify Instance')
    
    _sql_constraints = [
        ('product_id_shopify_instance_unique', 
         'UNIQUE(odoo_id, shopify_instance_id)', 
         'La combinación de Producto en Odoo y Shopify Instance debe ser única.')
    ]

class ShopifyProductTemplateMap(models.Model):
    _name = 'shopify.product.template.map'
    _description = 'Mapping del product template entre la web y Odoo'

    web_product_id = fields.Char(string="ID del producto en la web", required=True)
    odoo_id = fields.Many2one('product.template', string="Product Template en Odoo", required=True, ondelete='cascade')
    shopify_instance_id = fields.Many2one('shopify.web', string='Shopify Instance')
    
    _sql_constraints = [
        ('product_template_id_shopify_instance_unique', 
         'UNIQUE(odoo_id, shopify_instance_id)', 
         'La combinación de Product Template en Odoo y Shopify Instance debe ser única.')
    ]

class ShopifyVariantMap(models.Model):
    _name = 'shopify.variant.map'
    _description = 'Mapping de variante entre la web y Odoo'

    web_variant_id = fields.Char(string="ID de la variante en la web", required=True)
    odoo_id = fields.Many2one('product.product', string="Variante en Odoo", required=True)
    shopify_instance_id = fields.Many2one('shopify.web', string='Shopify Instance')
    _sql_constraints = [
        ('variant_id_shopify_instance_unique', 
         'UNIQUE(odoo_id, shopify_instance_id)', 
         'La combinación de Variante en Odoo y Shopify Instance debe ser única.')
    ]
    
class ShopifyStockMapping(models.Model):
    _name = 'shopify.stock.map'
    _description = 'Mapping de stock entre la web y Odoo'

    web_stock_id = fields.Char(string="ID de stock en la web", required=True)
    odoo_id = fields.Many2one('product.product', string="Variante en Odoo", required=True)  # Cambiado de stock.quant a product.product porque el quant no existe hasta que hay moviemientos, mientras que el inventory existe desde que existe la variante
    shopify_instance_id = fields.Many2one('shopify.web', string='Shopify Instance', required=True)
    shopify_location_id = fields.Many2one('shopify.location', string='Shopify Location', required=True)  # Nueva referencia a ubicación
    
    _sql_constraints = [
        ('stock_id_shopify_instance_unique', 
         'UNIQUE(odoo_id, shopify_location_id)', 
         'La combinación de Quant en Odoo y Shopify Location debe ser única.')
    ]    
    
    @api.model
    def create(self, vals):
        # Crear el registro de shopify.stock.map
        record = super(ShopifyStockMapping, self).create(vals)
        # Obtener el stock.quant asociado al product_id (odoo_id) y la ubicación
        if record.odoo_id:
            quant_domain = [('product_id', '=', record.odoo_id.id)]
            if record.shopify_location_id.import_stock_warehouse_id:
                quant_domain.append(('location_id', '=', record.shopify_location_id.import_stock_warehouse_id.id))
            quants = self.env['stock.quant'].search(quant_domain)
            if quants:
                # Forzar recálculo de effective_export_date
                quants._compute_effective_export_date()
        return record
        
class ShopifyPartnerMap(models.Model):
    _name = 'shopify.partner.map'
    _description = 'Shopify Partner Map'
    _rec_name = 'shopify_partner_id'

    partner_id = fields.Many2one('res.partner', string='Partner', required=True, ondelete='cascade')
    shopify_partner_id = fields.Char(string='Shopify Partner ID', required=True)
    shopify_instance_id = fields.Many2one('shopify.web', string='Shopify Instance', required=True)

    _sql_constraints = [
        ('partner_id_shopify_instance_unique', 
         'UNIQUE(partner_id, shopify_instance_id)', 
         'La combinación de Partner y Shopify Instance debe ser única.')
    ]

class ShopifyOrderMap(models.Model):
    _name = 'shopify.order.map'
    _description = 'Shopify Order Map'

    order_id = fields.Many2one('sale.order', string='Order', required=True, ondelete='cascade')
    shopify_order_id = fields.Char(string='Shopify Order ID', required=True)
    shopify_instance_id = fields.Many2one('shopify.web', string='Shopify Instance', required=True)

    _sql_constraints = [
        ('order_id_shopify_instance_unique', 
         'UNIQUE(order_id, shopify_instance_id)', 
         'La combinación de Order y Shopify Instance debe ser única.')
    ]
    