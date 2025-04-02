# -*- coding: utf-8 -*-
# See LICENSE file for full copyright and licensing details.
import logging
from odoo import api, models, fields

logger = logging.getLogger(__name__)


class StockQuant(models.Model):
    _inherit = "stock.quant"

    shopify_stock_map_ids = fields.One2many(
        "shopify.stock.map",
        "odoo_id",
        string="Shopify Stock Mappings",
        help="Mappings to Shopify stock across multiple websites"
    )
    
    effective_export_date = fields.Datetime(
        string="Effective Export Date",
        compute="_compute_effective_export_date",
        store=True,  # Almacenado para optimizar búsquedas
        help="Máximo entre write_date y la fecha de creación del mapa de stock asociado."
    )
    
    @api.depends('write_date', 'shopify_stock_map_ids.create_date')
    def _compute_effective_export_date(self):
        for quant in self:
            # Fecha inicial: write_date del quant
            effective_date = quant.write_date or quant.create_date  # Fallback a create_date si write_date es None
            # Buscar el mapa de stock relevante (puede haber múltiples mapas por producto)
            if quant.shopify_stock_map_ids:
                # Filtrar mapas que correspondan a la misma ubicación (si aplica)
                relevant_maps = quant.shopify_stock_map_ids.filtered(
                    lambda m: m.shopify_location_id.import_stock_warehouse_id == quant.location_id
                )
                if relevant_maps:
                    # Tomar el create_date más reciente de los mapas relevantes
                    map_create_date = max(relevant_maps.mapped('create_date'))
                    if map_create_date and map_create_date > effective_date:
                        effective_date = map_create_date
            quant.effective_export_date = effective_date
            
    def create_inventory_adjustment_ept(self, product_qty_data, location_id, auto_apply=False, name=""):
        
        quant_list = self.env['stock.quant']
        if product_qty_data and location_id:
            for product_id, product_qty in product_qty_data.items():
                val = self.prepare_vals_for_inventory_adjustment(location_id, product_id, product_qty)
                logger.info("Product ID: %s and its Qty: %s" % (product_id, product_qty))
                quant_list += self.with_context(inventory_mode=True).create(val)
            if auto_apply and quant_list:
                quant_list.filtered(lambda x: x.product_id.tracking not in ['lot', 'serial']).with_context(
                    inventory_name=name).action_apply_inventory()
        return quant_list

    def prepare_vals_for_inventory_adjustment(self, location_id, product_id, product_qty):
       
        return {'location_id': location_id.id, 'product_id': product_id,
                'inventory_quantity': product_qty}                
    
    def _unlink_zero_quants(self):
        """
        Override the method to prevent deletion of zero quants
        that might be referenced by external connectors.
        """
        # Este método vacío sobrescribe la funcionalidad original
        # y evita que se eliminen los quants con cantidad cero
        return True                
