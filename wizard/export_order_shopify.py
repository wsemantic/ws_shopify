# -*- coding: utf-8 -*-

from odoo.exceptions import UserError
from odoo import models, api, _, fields


class OrderExportInstance(models.Model):
    _name = 'order.export.instance'
    _description = 'Order Export'

    shopify_instance_id = fields.Many2one('shopify.instance', string="Shopify Instance")
    update_order = fields.Boolean(string="Update Order")

    def order_instance_for_exp(self):
        instance_id = self.shopify_instance_id
        update = self.update_order
        self.env['sale.order'].export_orders_to_shopify(instance_id, update)

    @api.model
    def default_get(self, fields):
        res = super(OrderExportInstance, self).default_get(fields)
        try:
            instance = self.env['shopify.instance'].search([])[0]
        except Exception as error:
            raise UserError(_("Please create and configure shopify Instance"))

        if instance:
            res['shopify_instance_id'] = instance.id

        return res
