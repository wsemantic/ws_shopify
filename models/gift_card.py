# create a class for gift card from shopify
import requests
from odoo import api, fields, models, _
from odoo.exceptions import UserError

import logging

_logger = logging.getLogger(__name__)


class GiftCard(models.Model):
    _name = "gift.card"
    _description = "Gift Card"

    name = fields.Char(string="Name", required=False)
    code = fields.Char(string="Code", required=False)
    value = fields.Float(string="Initial Value", required=False)
    customer_id = fields.Many2one('res.partner', string="Customer")
    expiry_date = fields.Date(string="Expiry Date")
    state = fields.Selection([('active', 'Active'), ('inactive', 'Inactive')], string="State", default="active")
    shopify_instance_id = fields.Many2one('shopify.web', string='Shopify Instance')
    shopify_gift_card_id = fields.Char('Shopify Gift Card Id')
    is_shopify = fields.Boolean('Is Shopify', default=False)

    def import_gift_cards(self, shopify_instance_ids):
        if shopify_instance_ids == False:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active', '=', True)])
        for shopify_instance_id in shopify_instance_ids:
            url = self.get_card_url(shopify_instance_id, endpoint='gift_cards.json')
            access_token = shopify_instance_id.shopify_shared_secret
            headers = {
                "X-Shopify-Access-Token": access_token,
            }
            params = {
                "limit": 250,  # Adjust the page size as needed
                "page_info": None,
                "status": "enabled"
            }

            all_gift_cards = []
            while True:
                response = requests.get(url, headers=headers, params=params)
                if response.status_code == 200 and response.content:
                    gift_cards = response.json()
                    cards = gift_cards.get('gift_cards', [])
                    all_gift_cards.extend(cards)
                    page_info = gift_cards.get('page_info', {})
                    if 'has_next_page' in page_info and page_info['has_next_page']:
                        params['page_info'] = page_info['next_page']
                    else:
                        break
                else:
                    break

            if all_gift_cards:
                cards = self.create_gift_cards(all_gift_cards, shopify_instance_id)
                return cards
            else:
                _logger.info("Gift Cards not found in shopify store")
                return []

    def get_card_url(self, shopify_instance_id, endpoint):
        shop_url = "https://{}.myshopify.com/admin/api/{}/{}".format(shopify_instance_id.shopify_host,
                                                                     shopify_instance_id.shopify_version, endpoint)
        return shop_url

    def create_gift_cards(self, gift_cards, shopify_instance_id):
        card_list = []
        for card in gift_cards:
            card_vals = {
                'shopify_gift_card_id': card.get('id'),
                'name': card.get('name'),
                'shopify_instance_id': shopify_instance_id.id,
                'is_shopify':True,
                'value':card.get('initial_value')
            }
            # check if card is present
            card_id = self.env['gift.card'].sudo().search(
                [('shopify_gift_card_id', '=', card.get('id')),('shopify_instance_id','=',shopify_instance_id.id)],limit=1)
            if not card_id:
                # create card in odoo
                card_id = self.env['gift.card'].sudo().create(card_vals)
                self.env.cr.commit()
            card_list.append(card_id.id)

        return card_list
