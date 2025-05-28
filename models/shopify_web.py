#create a model for shopify instance

from odoo import api, fields, models, _
from odoo.exceptions import UserError
import logging
import requests,re

_logger = logging.getLogger(__name__)

class ShopifyInstance(models.Model):
    _name = 'shopify.web'
    _description = 'Shopify Instance'

    name = fields.Char('Name', required=True)
    shopify_api_key = fields.Char('API Key', required=True)
    shopify_password = fields.Char('Secret Key', required=True)
    shopify_shared_secret = fields.Char('Access Token', required=True)
    shopify_host = fields.Char('Shopify Host', required=True)
    shopify_active = fields.Boolean('Active', default=False)
    shopify_version = fields.Char('Shopify Version')
    shopify_warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse')
    shopify_company_id = fields.Many2one('res.company', string='Company')
    shopify_last_date_customer_import = fields.Datetime('Last Date Customer Import')
    shopify_last_date_update_stock = fields.Datetime('Last Date Update Stock')
    shopify_last_date_product_import = fields.Datetime('Last Date Product Import')
    shopify_last_date_order_import = fields.Datetime('Last Date Order Import')
    shopify_last_date_draftorder_import = fields.Datetime('Last Date Draft Order Import')
    payout_last_import_date = fields.Datetime('Last Date Payout Import')
    
    last_export_customer = fields.Datetime(string="Última exportación de clientes")
    last_export_product = fields.Datetime(string="Última exportación de productos")
    last_export_stock = fields.Datetime(string="Última actualización de stock")
    split_products_by_color = fields.Boolean(string="Split Products by Color", default=False)
    color_option_position = fields.Integer(string="Color Option Position", default=1, help="Define en qué opción de Shopify se mapeará el color (por defecto, en la opción 1).")
    size_option_position = fields.Integer(string="Size Option Position", default=2, help="Define en qué opción de Shopify se mapeará la talla (por defecto, en la opción 2).")
    
    last_export_stock_id = fields.Integer(string="Último ID Stock exportado", default=0, help="ID del último stock exportado")
    last_export_product_id = fields.Integer(string="Último ID Producto exportado", default=0, help="ID del último producto exportado")
    shopify_last_import_customer_id = fields.Char(string="Último ID Cliente importado", help="ID del último Cliente importado")
    salesperson_id = fields.Many2one(
        'res.users', 
        string='Salesperson', 
        help='Default salesperson assigned to customers and orders from this Shopify instance.'
    )

    prices_include_tax = fields.Boolean(
        string='Prices Include Tax', 
        default=True, 
        help='Indicate if prices in Shopify include taxes. If unchecked, prices are assumed to be tax-excluded and will not be adjusted during order import.'
    )

    # create a method to authenticate with shopify instance
    def shopify_authenticate(self, vals=False):
        # authenticate with shopify instance
        connection = self.connect_in_shopify(vals)
        if connection == True:
            # try:
            #     shop = shopify.Shop.current()
            #     _logger.info("Successfully authenticated with shopify instance")
            # except Exception as e:
            #     _logger.error("Error while authenticating with shopify instance")
            #     raise UserError(_("Error while authenticating with shopify instance"))
            raise UserError(_("Successfully authenticated with shopify instance"))
        else:
            raise UserError(_("Error while authenticating with shopify instance"))


    def connect_in_shopify(self, vals=False):
        if vals:
            api_key = vals.get("shopify_api_key")
            password = vals.get("shopify_password")
            shopify_host = vals.get("shopify_host")
            shopify_version = vals.get("shopify_version")
            shopify_shared_secret = vals.get("shopify_shared_secret")
        else:
            api_key = self.shopify_api_key
            password = self.shopify_password
            shopify_host = self.shopify_host
            shopify_version = self.shopify_version
            shopify_shared_secret = self.shopify_shared_secret

        # shop_url = self.prepare_shopify_shop_url(shopify_host, api_key, password, shopify_version)

        # Create a session
        session = requests.Session()
        session.auth = (api_key, password)
        headers = {
            "Content-Type": "application/json",
            "X-Shopify-Access-Token": shopify_shared_secret
        }

        endpoint = f"https://{shopify_host}.myshopify.com/admin/api/{shopify_version}/shop.json"

        response = session.get(endpoint, headers=headers)
        if response.status_code == 200:
            connection = True
        else:
            connection = False
        # try:
        #     session = shopify.Session(shop_url,shopify_version)
        #     print("Session------------------",session)
        #     session.api_key = api_key
        #     session.password = password
        #     session.token = shopify_shared_secret
        #     shopify.ShopifyResource.set_site(shop_url)
        #     print("-------------------",shopify.ShopifyResource.set_site(shop_url))
        #     shopify.ShopifyResource.activate_session(session)
        #     print("==================",shopify.ShopifyResource.activate_session(session))
        #     connection = True
        # except Exception as e:
        #     print("error====================",e)
        #     _logger.info("{}".format(e))
        #     connection = False
        return connection


    def prepare_shopify_shop_url(self, host, api_key, password,version):
        shop_url = "https://{}:{}@{}.myshopify.com/admin/api/{}".format(api_key,password,host,version)
        return shop_url

    def _parse_link_header(self,link_header):
        # Busca patrones del tipo:
        # <URL>; rel="next", <URL>; rel="previous", etc.
        pattern = r'<([^>]+)>;\s*rel="(\w+)"'
        matches = re.findall(pattern, link_header)
        # matches será lista de tuplas [(url, rel), (url, rel), ...]
        links = {}
        for url, rel in matches:
            links[rel] = url
        return links    
        
    def clean_string(self,text):
        """
        Elimina los backslashes que generan secuencias de escape no deseadas,
        excepto aquellas que formen parte de secuencias válidas (por ejemplo, \n, \t, etc.).
        En este ejemplo, se reemplaza cualquier '\' que no esté seguido por 'n', 't', 'r' o '\' por una cadena vacía.
        """
        # Esta expresión regular busca un '\' que no vaya seguido de n, t, r o \
        cleaned = re.sub(r'\\(?![ntr\\])', '', text)
        return cleaned