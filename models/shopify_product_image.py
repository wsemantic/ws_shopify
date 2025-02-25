# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.

import logging
from odoo import models, fields

_logger = logging.getLogger("Shopify Image")


class ShopifyProductImage(models.Model):
    
    _name = "shopify.product.image"
    _description = "Shopify Product Image"
    _order = "create_date desc, id"

    shopify_image_id = fields.Char(string="Shopify Image ID", help="Id of image in Shopify.")
    shopify_variant_id = fields.Many2one("product.product")
    shopify_template_id = fields.Many2one("product.template")
    url = fields.Char(string="URL", help="External URL of image")
    shopify_image = fields.Binary(string="Shopify Image", help="Image of product in Shopify")
    image = fields.Image()





