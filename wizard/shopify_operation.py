# create a wizard model to perform shopify operations

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from datetime import datetime, timedelta

import logging

_logger = logging.getLogger(__name__)


class ShopifyOperation(models.TransientModel):
    _name = 'shopify.operation'
    _description = 'Shopify Operation'

    # initialize fields
    shopify_instance_id = fields.Many2one('shopify.web', string='Shopify Instance')
    import_export_selection = fields.Selection([('import', 'Import'), ('export', 'Export')], string="Import/Export",
                                               default='import')
    shopify_operation = fields.Selection(
        [('import_shopify_customers', 'Import Customers'),
         ('import_shopify_products', 'Import Products'),
         ('import_locations', 'Import Locations'),
         ('update_stock', 'Update Stock'),
         # ('import_draft_orders', 'Import Draft Orders'),
         ('import_shopify_orders', 'Import Orders'),
         # ('import_gift_cards','Import Gift Cards'),
         # ('import_payouts', 'Import Payouts')
         ], default='import_shopify_customers', string='Import Operations')

    export_shopify_operation = fields.Selection(
        [('export_shopify_customers', 'Export Customers'),
         ('export_shopify_products', 'Export Products'),
         ('export_shopify_orders', 'Export Orders'),
         ('export_shopify_stock', 'Export Stock')], default='export_shopify_customers', string='Export Operations')

    
    orders_from_date = fields.Datetime(string="From Date")
    orders_to_date = fields.Datetime(string="To Date")
    date_filter = fields.Boolean(string="Date Filter")
    skip_existing_product = fields.Boolean(string="Do Not Update Existing Products",
                                           help="Check if you want to skip existing products.")
    skip_existing_customer = fields.Boolean(string="Do Not Update Existing Customers",
                                            help="Check if you want to skip existing customers.")
    skip_existing_order = fields.Boolean(string="Do Not Update Existing Orders",
                                         help="Check if you want to skip existing orders.")

    # create method to perform shopify operations
    def perform_shopify_operation(self):
        ids = False
        # check if shopify operation is import shopify customers
        if self.shopify_operation == 'import_shopify_customers':
            # call method in res.partner model to import customers from shopify to odoo
            customers = self.env['res.partner'].import_shopify_customers(self.shopify_instance_id,
                                                                         self.skip_existing_customer)
            if customers:
                
                ids = customers
                action_name = "ws_shopify.action_shopify_customer"
        elif self.shopify_operation == 'import_shopify_products':
            # call method to import products from shopify to odoo
            products = self.env['product.template'].import_shopify_products(self.shopify_instance_id,
                                                                            self.skip_existing_product,
                                                                            self.orders_from_date, self.orders_to_date)
            if products:
                self.shopify_instance_id.shopify_last_date_product_import = datetime.now()
                ids = products
                action_name = "ws_shopify.action_product_template_shopify"
        elif self.shopify_operation == 'import_locations':
            locations = self.env['shopify.location'].import_shopify_locations(self.shopify_instance_id)
            if locations:
                ids = locations
                action_name = "ws_shopify.shopify_location_action"
        elif self.shopify_operation == 'update_stock':
            products = self.env['product.template'].update_stock(self.shopify_instance_id)
            if products:
                ids = products
                action_name = "ws_shopify.action_product_product_shopify"
        elif self.shopify_operation == 'import_draft_orders':
            # call method to import draft orders from shopify to odoo
            draft_orders = self.env['sale.order'].import_shopify_draft_orders(self.shopify_instance_id,
                                                                              self.skip_existing_order,
                                                                              self.orders_from_date,
                                                                              self.orders_to_date)
            if draft_orders:
                self.shopify_instance_id.shopify_last_date_draftorder_import = datetime.now()
                ids = draft_orders
                action_name = "ws_shopify.action_order_quotation_shopify"
        elif self.shopify_operation == 'import_shopify_orders':
            # call method to import orders from shopify to odoo
            orders = self.env['sale.order'].import_shopify_orders(self.shopify_instance_id,
                                                                  self.skip_existing_order,
                                                                  self.orders_from_date,
                                                                  self.orders_to_date)
            if orders:
                self.shopify_instance_id.shopify_last_date_order_import = datetime.now()
                ids = orders
                action_name = "ws_shopify.action_sale_order_shopify"


        elif self.shopify_operation == 'import_gift_cards':
            # call method to import gift cards from shopify to odoo
            gift_cards = self.env['gift.card'].import_gift_cards(self.shopify_instance_id)
            if gift_cards:
                ids = gift_cards
                action_name = "ws_shopify.shopify_gift_card_action"

        elif self.shopify_operation == 'import_payouts':
            # call method to import payouts from shopify to odoo
            payouts = self.env['shopify.payout'].import_payouts(self.shopify_instance_id)
            if payouts:
                ids = payouts
                action_name = "ws_shopify.shopify_payouts_action"

        if ids and action_name:
            action = self.env.ref(action_name).sudo().read()[0]
            action["domain"] = [("id", "in", ids)]
            return action

        return {
            "type": "ir.actions.client",
            "tag": "reload",
        }

    def perform_export_shopify_operation(self):
        ids = False
        # check if shopify operation is import shopify customers
        if self.export_shopify_operation == 'export_shopify_customers':
            # call method in res.partner model to export customers to shopify from odoo
            customers = self.env['res.partner'].export_customers_to_shopify(self.shopify_instance_id,False)
        elif self.export_shopify_operation == 'export_shopify_products':
            # call method in res.partner model to export customers to shopify from odoo
            products = self.env['product.template'].export_products_to_shopify(self.shopify_instance_id,False)
        elif self.export_shopify_operation == 'export_shopify_orders':
            # call method in res.partner model to export customers to shopify from odoo
            orders = self.env['sale.order'].export_orders_to_shopify(self.shopify_instance_id,False)
        elif self.export_shopify_operation == 'export_shopify_stock':
            products = self.env['product.template'].export_stock_to_shopify(self.shopify_instance_id)
            if products:
                action = self.env.ref("ws_shopify.action_product_product_shopify").sudo().read()[0]
                action["domain"] = [("id", "in", products)]
                return action
            else:
                return {
                    "type": "ir.actions.client",
                    "tag": "reload",
                }
