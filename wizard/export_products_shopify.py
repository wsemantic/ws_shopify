# -*- coding: utf-8 -*-

from odoo.exceptions import UserError
from odoo import models, api, _, fields


class ProductExportInstance(models.TransientModel):
    _name = 'product.export.instance'
    _description = 'Products Export'

    shopify_instance_id = fields.Many2one('shopify.web', string="Shopify Instance", required=True)
    update_products = fields.Boolean(string="Update Products")

    def product_instance_for_exp(self):
        """Exporta los productos seleccionados a la instancia Shopify elegida."""
        instance_id = self.shopify_instance_id
        update = self.update_products
        selected_products = self.env['product.template'].browse(self.env.context.get('active_ids', []))
        
        if not selected_products:
            raise UserError(_("No products selected for export. Please select at least one product."))
        
        # Llamar al método de exportación con los productos seleccionados
        self.env['product.template'].export_products_to_shopify([instance_id], update=update, products=selected_products)
        return {'type': 'ir.actions.act_window_close'}

    @api.model
    def default_get(self, fields):
        res = super(ProductExportInstance, self).default_get(fields)
        try:
            instance = self.env['shopify.web'].search([('shopify_active', '=', True)], limit=1)
        except Exception as error:
            raise UserError(_("Please create and configure a Shopify instance"))
        
        if instance:
            res['shopify_instance_id'] = instance.id

        return res
