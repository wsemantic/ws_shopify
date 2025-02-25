# -*- coding: utf-8 -*-
from odoo import models, fields, api

class ShopifyProductMap(models.Model):
    _name = 'shopify.product.map'
    _description = 'Mapping del producto entre la web y Odoo'

    web_product_id = fields.Char(string="ID del producto en la web", required=True)
    odoo_product_id = fields.Many2one('product.template', string="Producto en Odoo", required=True)
    shopify_instance_id = fields.Many2one('shopify.instance', string='Shopify Instance')

class ShopifyVariantMap(models.Model):
    _name = 'shopify.variant.map'
    _description = 'Mapping de variante entre la web y Odoo'

    web_variant_id = fields.Char(string="ID de la variante en la web", required=True)
    odoo_variant_id = fields.Many2one('product.product', string="Variante en Odoo", required=True)
    shopify_instance_id = fields.Many2one('shopify.instance', string='Shopify Instance')

class ShopifyStockMapping(models.Model):
    _name = 'shopify.stock.map'
    _description = 'Mapping de stock entre la web y Odoo'

    web_stock_id = fields.Char(string="ID de stock en la web", required=True)
    odoo_quant_id = fields.Many2one('stock.quant', string="Quant en Odoo", required=True)
    shopify_instance_id = fields.Many2one('shopify.instance', string='Shopify Instance')
