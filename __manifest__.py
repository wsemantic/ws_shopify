# -*- coding: utf-8 -*-
{
    'name': 'WS Shopify Connector',
    'version': '16.0.01',
    'category': 'Services',
    'author': 'Semantic Web Software',
    'website': 'wsemantic.com',
    'summary': 'This module is used to connect odoo with shopify',
    'description': """
       Odoo-shopify connector: This module is used to connect odoo with shopify.
    """,
    'depends': ['base', 'sale', 'sale_management', 'product', 'stock', 'delivery'],

    'data': [
        'security/ir.model.access.csv',
        'data/ir_cron_data.xml'

    ],
    'images': ['static/description/shopify_connector_app.gif'],
    'license': 'OPL-1',
    'installable': True,
    'application': True,
    'auto_install': False,
}
