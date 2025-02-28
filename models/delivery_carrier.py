# -*- coding: utf-8 -*-

from odoo import fields, models, _
from odoo.exceptions import UserError

class DeliveryCarrier(models.Model):
    _inherit = 'delivery.carrier'

    shopify_instance_id = fields.Many2one('shopify.web', string='Shopify Instance')
    is_shopify = fields.Boolean('Shopify', default=False)
