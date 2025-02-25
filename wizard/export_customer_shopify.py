# -*- coding: utf-8 -*-

from odoo.exceptions import UserError
from odoo import models, api, _, fields


class CustomerExportInstance(models.Model):
    _name = 'customer.export.instance'
    _description = 'Customer Export'

    shopify_instance_id = fields.Many2one('shopify.instance', string="Shopify Instance")
    update_customer = fields.Boolean(string="Update Customer",default=False)

    def customer_instance_for_exp(self):
        instance_id = self.shopify_instance_id
        update = self.update_customer
        self.env['res.partner'].export_customers_to_shopify(instance_id,update)

    @api.model
    def default_get(self, fields):
        res = super(CustomerExportInstance, self).default_get(fields)
        try:
            instance = self.env['shopify.instance'].search([])[0]
        except Exception as error:
            raise UserError(_("Please create and configure shopify Instance"))

        if instance:
            res['shopify_instance_id'] = instance.id

        return res
