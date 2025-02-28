# create a class for gift card from shopify
import requests
from odoo import api, fields, models, _
from odoo.exceptions import UserError

import logging

_logger = logging.getLogger(__name__)


class ShopifyPayout(models.Model):
    _name = "shopify.payout"
    _description = "Shopify Payout"

    name = fields.Char(string="Name", required=False)
    amount = fields.Float(string="Amount", required=False)
    shopify_id = fields.Char(string="Shopify Payout ID")
    status = fields.Selection([('scheduled', 'Scheduled'), ('in_transit', 'In Transit'), ('paid', 'Paid'),
                               ('failed', 'Failed'), ('cancelled', 'Cancelled')], string="Status")
    shopify_instance_id = fields.Many2one('shopify.web', string="Shopify Instance")
    is_shopify = fields.Boolean(string="Is Shopify")
    state = fields.Selection([('draft', 'Draft'), ('partially_generated', 'Partially Generated'),
                              ('generated', 'Generated'), ('partially_processed', 'Partially Processed'),
                              ('processed', 'Processed'), ('validated', 'Validated')], string="Status",
                             default="draft", tracking=True)
    payout_date = fields.Date(help="The date the payout was issued.")
    currency_id = fields.Many2one('res.currency', string='Currency',
                                  help="currency code of the payout.")

    def import_payouts(self, shopify_instance_ids):
        if shopify_instance_ids == False:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active', '=', True)])
        for shopify_instance_id in shopify_instance_ids:
            url = self.get_payout_url(shopify_instance_id, endpoint='shopify_payments/payouts.json')
            access_token = shopify_instance_id.shopify_shared_secret
            headers = {
                "X-Shopify-Access-Token": access_token,
            }
            params = {
                "limit": 250,  # Adjust the page size as needed
                "page_info": None,
            }

            all_payouts = []
            while True:
                response = requests.get(url, headers=headers, params=params)
                if response.status_code == 200 and response.content:
                    payouts = response.json()
                    cards = payouts.get('payouts', [])
                    all_payouts.extend(cards)
                    page_info = payouts.get('page_info', {})
                    if 'has_next_page' in page_info and page_info['has_next_page']:
                        params['page_info'] = page_info['next_page']
                    else:
                        break
                else:
                    break

            if all_payouts:
                payouts = self.create_payouts(all_payouts, shopify_instance_id)
                return payouts
            else:
                _logger.info("Payouts not found in shopify store")
                return []

    def get_payout_url(self, shopify_instance_id, endpoint):
        shop_url = "https://{}.myshopify.com/admin/api/{}/{}".format(shopify_instance_id.shopify_host,
                                                                     shopify_instance_id.shopify_version, endpoint)
        return shop_url

    def create_payouts(self, payouts, shopify_instance_id):
        payout_list = []
        for payout in payouts:
            payout_vals = {
                'shopify_id': payout.get('id'),
                'shopify_instance_id': shopify_instance_id.id,
                'is_shopify': True,
                'amount': payout.get('amount')
            }
            # check if payout is present
            payout_id = self.env['shopify.payout'].sudo().search(
                [('shopify_id', '=', payout.get('id')), ('shopify_instance_id', '=', shopify_instance_id.id)], limit=1)
            if not payout_id:
                # create payout in odoo
                payout_id = self.env['shopify.payout'].sudo().create(payout_vals)
                self.env.cr.commit()
            payout_list.append(payout_id.id)

        return payout_list
