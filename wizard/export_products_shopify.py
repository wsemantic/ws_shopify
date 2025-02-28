# -*- coding: utf-8 -*-

from odoo.exceptions import UserError
from odoo import models, api, _, fields


class ProductExportInstance(models.Model):
    _name = 'product.export.instance'
    _description = 'Products Export'

    shopify_instance_id = fields.Many2one('shopify.web', string="Shopify Instance")
    update_products = fields.Boolean(string="Update Products")

    def product_instance_for_exp(self):
        instance_id = self.shopify_instance_id
        update = self.update_products
        self.env['product.template'].export_products_to_shopify(instance_id, update)

    @api.model
    def default_get(self, fields):
        res = super(ProductExportInstance, self).default_get(fields)
        try:
            instance = self.env['shopify.web'].search([])[0]
        except Exception as error:
            raise UserError(_("Please create and configure shopify Instance"))

        if instance:
            res['shopify_instance_id'] = instance.id

        return res
