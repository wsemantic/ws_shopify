# create a model for shopify instance
import requests
from odoo import api, fields, models, _
import logging

_logger = logging.getLogger(__name__)


class ShopifyLocation(models.Model):
    _name = 'shopify.location'
    _description = 'Shopify Location'

    name = fields.Char('Name', required=True)
    shopify_location_id = fields.Char('Shopify Location ID')
    shopify_instance_id = fields.Many2one('shopify.web', string="Shopify Instance")
    legacy = fields.Boolean('Is Legacy Location', help="Whether this is a fulfillment service location. If true, then"
                                                       "the location is a fulfillment service location. If false, then"
                                                       "the location was created by the merchant and isn't tied to a"
                                                       "fulfillment service.", readonly="1")
    is_primary_location = fields.Boolean(readonly="1")
    shopify_instance_company_id = fields.Many2one('res.company', string='Company', readonly=True)
    active = fields.Boolean(default=True)
    is_shopify = fields.Boolean(default=False,string="Is shopify")
    stock_warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse',
                                                help="Selected warehouse used while Import the stock.")

    def import_shopify_locations(self, shopify_instance_ids):
        if shopify_instance_ids == False:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active','=',True)])
        for shopify_instance_id in shopify_instance_ids:
            url = self.get_location_url(shopify_instance_id, endpoint='locations.json')
            access_token = shopify_instance_id.shopify_shared_secret
            headers = {
                "X-Shopify-Access-Token": access_token,
            }
            params = {
                "limit": 250,  # Adjust the page size as needed
                "page_info": None
            }

            all_locations = []
            while True:
                response = requests.get(url, headers=headers, params=params)
                if response.status_code == 200 and response.content:
                    shopify_locations = response.json()
                    locations = shopify_locations.get('locations', [])
                    all_locations.extend(locations)
                    page_info = shopify_locations.get('page_info', {})
                    if 'has_next_page' in page_info and page_info['has_next_page']:
                        params['page_info'] = page_info['next_page']
                    else:
                        break
                else:
                    break

            if all_locations:
                locations = self.create_locations(all_locations, shopify_instance_id)
                return locations
            else:
                _logger.info("Locations not found in shopify store")
                return []

    def get_location_url(self, shopify_instance_id, endpoint):
        shop_url = "https://{}.myshopify.com/admin/api/{}/{}".format(shopify_instance_id.shopify_host,
                                                                     shopify_instance_id.shopify_version, endpoint)
        return shop_url

    def create_locations(self, locations, shopify_instance_id):
        location_list = []
        for shopify_location in locations:
            location_vals = {
                'shopify_location_id': shopify_location.get('id'),
                'name': shopify_location.get('name'),
                'shopify_instance_id': shopify_instance_id.id,
                'is_shopify':True,
            }
            # check if location is present
            location = self.env['shopify.location'].sudo().search(
                [('shopify_location_id', '=', shopify_location.get('id')),('shopify_instance_id','=',shopify_instance_id.id)],limit=1)
            if not location:
                # create location in odoo
                location = self.env['shopify.location'].sudo().create(location_vals)
                self.env.cr.commit()
            location_list.append(location.id)

        return location_list