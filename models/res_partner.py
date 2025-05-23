# inherit res.partner model with fields to store shopify customer id and shopify instance id
import json
import re
import requests
from odoo import api, fields, models, _
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

class ResPartner(models.Model):
    _inherit = 'res.partner'

    shopify_partner_map_ids = fields.One2many(
        'shopify.partner.map',
        'partner_id',
        string='Shopify Mappings'
    )
    
    shopify_exported = fields.Boolean(
        string="Exportado",
        compute="_compute_shopify_exported",
        store=True
    )

    @api.depends('shopify_partner_map_ids')
    def _compute_shopify_exported(self):
        for partner in self:
            partner.shopify_exported = bool(partner.shopify_partner_map_ids)

    def import_shopify_customers(self, shopify_instance_ids, skip_existing_customer):
        if not shopify_instance_ids:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active', '=', True)])

        _logger.info("WSSH Import customer %i instancias", len(shopify_instance_ids))
        for shopify_instance_id in shopify_instance_ids:
            _logger.info("WSSH Dentro de instance %s", shopify_instance_id.name)
            url = self.get_customer_url(shopify_instance_id, endpoint='customers.json')
            access_token = shopify_instance_id.shopify_shared_secret
            headers = {"X-Shopify-Access-Token": access_token}
            params = {"limit": 250, "pageInfo": None}
            if shopify_instance_id.shopify_last_date_customer_import:
                params["created_at_min"] = shopify_instance_id.shopify_last_date_customer_import

            all_customers = []
            while True:
                _logger.info("WSSH Iteración response")
                response = requests.get(url, headers=headers, params=params)
                if response.status_code == 200 and response.content:
                    shopify_customers = response.json()
                    customers = shopify_customers.get('customers', [])
                    all_customers.extend(customers)
                    _logger.info(f"WSSH Iteración response n {len(all_customers)}")
                    link_header = response.headers.get('Link')
                    if link_header:
                        links = shopify_instance_id._parse_link_header(link_header)
                        if 'next' in links:
                            url = links['next']
                            params = None
                            continue
                break
            _logger.info("WSSH Found %d customers to import for instance %s", len(all_customers), shopify_instance_id.name)
            
            if all_customers:
                # Obtener metafields para cada cliente
                for customer in all_customers:
                    customer['metafields'] = self.get_customer_metafields(customer.get('id'), shopify_instance_id)
                
                customer_ids = self.create_customers(all_customers, shopify_instance_id, skip_existing_customer)
                shopify_instance_id.shopify_last_date_customer_import = fields.Datetime.now()
                return customer_ids
            else:
                _logger.info("WSSH No customers found in Shopify store for instance %s", shopify_instance_id.name)
                return []

    def get_customer_url(self, shopify_instance_id, endpoint):
        return f"https://{shopify_instance_id.shopify_host}.myshopify.com/admin/api/{shopify_instance_id.shopify_version}/{endpoint}"

    def get_customer_metafields(self, customer_id, shopify_instance_id):
        """Obtiene los metafields de un cliente específico"""
        if not customer_id:
            return {}
            
        url = self.get_customer_url(shopify_instance_id, endpoint=f'customers/{customer_id}/metafields.json')
        headers = {"X-Shopify-Access-Token": shopify_instance_id.shopify_shared_secret}
        
        response = requests.get(url, headers=headers)
        if response.status_code == 200 and response.content:
            metafields_data = response.json().get('metafields', [])
            metafields = {}
            for field in metafields_data:
                if field.get('namespace') == 'custom':
                    key=field.get('key')
                    if 'referencia' in key:
                        key='ref'
                    value=field.get('value')
                    metafields[key] = value
                    _logger.info(f"WSSH Metafield {key} {value}")
            return metafields
        return {}

    def prepare_customer_vals(self, shopify_customer, shopify_instance_id):
        """Prepara los valores para crear o actualizar un cliente."""
                                                  
        addresses = shopify_customer.get('default_address', {})
        first_address = addresses
        metafields = shopify_customer.get('metafields', {})
                                                                             
        first_name = shopify_customer.get('first_name') or first_address.get('first_name') or ''
        last_name = shopify_customer.get('last_name') or first_address.get('last_name') or ''
        email = shopify_customer.get('email') or first_address.get('email') or ''
        phone = shopify_customer.get('phone') or first_address.get('phone') or ''
        name = self._get_customer_name(first_name, last_name, email)                                                                        

        street = first_address.get('address1')
        street2 = first_address.get('address2')
        city = first_address.get('city')
        zip = first_address.get('zip')
        country_code = first_address.get('country_code')
        country_id = self.env['res.country'].sudo().search([('code', '=', country_code)], limit=1).id if country_code else False

        vals = {
            'name': name,
            'customer_rank': 1,
            'email': email,
            'phone': phone,
            'ref': metafields.get('ref') or ('SID' + str(shopify_customer.get('id'))),
            'street': street,
            'street2': street2,
            'city': city,
            'zip': zip,
            'country_id': country_id,
            'user_id': shopify_instance_id.salesperson_id.id if shopify_instance_id.salesperson_id else False,  # Asignar comercial
        }
        
        # Añadir metafields adicionales
        if 'vat' in metafields:
            vals['vat'] = metafields.get('vat')
            
        return vals

    def create_customers(self, shopify_customers, shopify_instance_id, skip_existing_customer):
        customer_list = []
        for shopify_customer in shopify_customers:
            partner = self._find_existing_partner(shopify_customer, shopify_instance_id)
            if partner:
                _logger.info(f"WSSH Partner existente encontrado {partner.name} id {shopify_customer.get('id')} skip {skip_existing_customer}")
                mapping = partner.shopify_partner_map_ids.filtered(lambda m: m.shopify_instance_id == shopify_instance_id)
                if mapping:
                    mapping.write({'shopify_partner_id': shopify_customer.get('id')})
                else:
                    self.env['shopify.partner.map'].create({
                        'partner_id': partner.id,
                        'shopify_partner_id': shopify_customer.get('id'),
                        'shopify_instance_id': shopify_instance_id.id,
                    })            
                    if skip_existing_customer:
                        partner.write({'user_id':shopify_instance_id.salesperson_id.id if shopify_instance_id.salesperson_id else False})  # Asignar comercial                    
                if not skip_existing_customer:
                    vals_update = self.prepare_customer_vals(shopify_customer, shopify_instance_id)
                    partner.with_context(no_vat_validation=True).write(vals_update)
                
            else:
                _logger.info(f"WSSH Partner NO encontrado id {shopify_customer.get('id')}")
                vals = self.prepare_customer_vals(shopify_customer, shopify_instance_id)  
                partner = super(ResPartner, self).with_context(no_vat_validation=True).create(vals)
                self.env['shopify.partner.map'].create({
                    'partner_id': partner.id,
                    'shopify_partner_id': shopify_customer.get('id'),
                    'shopify_instance_id': shopify_instance_id.id,
                })
            
            customer_list.append(partner.id)
        
        return customer_list

    def _get_customer_name(self, first_name, last_name, email):
        """Genera el nombre del cliente a partir de first_name y last_name, con email como fallback."""
        name = (first_name + ' ' + last_name).strip()
        return name or email or _("Shopify Customer")

    def _find_existing_partner(self, shopify_customer, shopify_instance_id):
        shopify_customer_id = shopify_customer.get('id')
        addresses = shopify_customer.get('default_address', {})
        first_address = addresses 
        metafields = shopify_customer.get('metafields', {})

        # Buscar por mapping en shopify_partner_map_ids
        partner = self.env['res.partner'].search([
            ('shopify_partner_map_ids.shopify_partner_id', '=', shopify_customer_id),
            ('shopify_partner_map_ids.shopify_instance_id', '=', shopify_instance_id.id)
        ], limit=1)
        if partner:
            return partner

        # Obtener datos para búsqueda por prioridad
        ref_value = metafields.get('ref')
        _logger.info(f"WSSH Find Partner REF {ref_value}")
        email = shopify_customer.get('email') or first_address.get('email')
        phone = shopify_customer.get('phone') or first_address.get('phone')

        # Limpiar y validar campos
        if email: email = shopify_instance_id.clean_string(email)
        if phone: phone = shopify_instance_id.clean_string(phone)

        if email and not self._is_valid_email(email):
            _logger.warning("El email '%s' no es válido y se omite en la búsqueda", email)
            email = None
        if phone and not self._is_valid_phone(phone):
            _logger.warning("El teléfono '%s' no es válido y se omite en la búsqueda", phone)
            phone = None

        # Construir dominio de búsqueda por orden de prioridad: ref, email, phone
        or_conditions = []
        if ref_value: or_conditions.append(('ref', '=', ref_value))
        if email: or_conditions.append(('email', '=', email))
        if phone: or_conditions.append(('phone', '=', phone))

        if not or_conditions:
            return None
        
        # Construir el OR completo
        domain = []
        if len(or_conditions) > 1:
            domain = ['|'] * (len(or_conditions) - 1)
        domain.extend(or_conditions)
        
        return self.search(domain, limit=1)

    def _is_valid_email(self, email):
        pattern = r'^[\w\.\-\+]+@[\w\.-]+\.\w+$'
        return isinstance(email, str) and re.match(pattern, email)

    def _is_valid_phone(self, phone):
        pattern = r'^[\d\+\-\s\(\)]+$'
        return isinstance(phone, str) and re.match(pattern, phone)

    def get_or_create_partner_from_shopify(self, shopify_customer, shopify_instance_id):
        """Busca o crea un partner basado en datos de Shopify, reutilizable desde sale.order."""
        # Obtener metafields si no existen
        if 'metafields' not in shopify_customer:
            shopify_customer['metafields'] = self.get_customer_metafields(shopify_customer.get('id'), shopify_instance_id)
            
        partner = self._find_existing_partner(shopify_customer, shopify_instance_id)
        if not partner:
            vals = self.prepare_customer_vals(shopify_customer, shopify_instance_id)
            partner = super(ResPartner, self).with_context(no_vat_validation=True).create(vals)
            self.env['shopify.partner.map'].create({
                'partner_id': partner.id,
                'shopify_partner_id': shopify_customer.get('id'),
                'shopify_instance_id': shopify_instance_id.id,
            })
            _logger.info(f"WSSH Creado nuevo partner desde Shopify: {partner.name}")
        else:
            _logger.info(f"WSSH Partner existente encontrado desde Shopify: {partner.name}")
        return partner

    def export_customers_to_shopify(self, shopify_instance_ids, update):
        partner_ids = self.sudo().browse(self._context.get("active_ids"))
        if not partner_ids:
            domain = []
            for instance_id in shopify_instance_ids:
                if instance_id.last_export_customer:
                    domain.append(('write_date', '>=', instance_id.last_export_customer))
            partner_ids = self.sudo().search(domain)

        if not shopify_instance_ids:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active', '=', True)])

        for instance_id in shopify_instance_ids:
            url = self.get_customer_url(instance_id, endpoint='customers.json')
            headers = {
                "X-Shopify-Access-Token": instance_id.shopify_shared_secret,
                "Content-Type": "application/json"
            }

            for partner in partner_ids:
                tag_vals = ','.join(tag.name for tag in partner.category_id) if partner.category_id else ''
                mapping = partner.shopify_partner_map_ids.filtered(lambda m: m.shopify_instance_id == instance_id)
                
                data = {
                    "customer": {
                        "email": partner.email or "",
                        "phone": partner.phone or "",
                        "tags": tag_vals,
                        "addresses": [{
                            "address1": partner.street or "",
                            "city": partner.city or "",
                            "phone": partner.phone or "",
                            "zip": partner.zip or "",
                            "first_name": partner.name or "",
                            "last_name": "",
                            "country": partner.country_id.code if partner.country_id else ""
                        }]
                    }
                }

                if mapping and update:
                    data["customer"]["id"] = mapping.shopify_partner_id
                    url = self.get_customer_url(instance_id, endpoint=f'customers/{mapping.shopify_partner_id}.json')
                    response = requests.put(url, headers=headers, data=json.dumps(data))
                else:
                    data["customer"].update({
                        "first_name": partner.name or "",
                        "last_name": "",
                        "verified_email": True,
                        "send_email_welcome": False
                    })
                    response = requests.post(url, headers=headers, data=json.dumps(data))

                if response and response.ok:
                    shopify_customer = response.json().get('customer', {})
                    if shopify_customer:
                        if mapping:
                            mapping.write({'shopify_partner_id': shopify_customer.get('id')})
                        else:
                            self.env['shopify.partner.map'].create({
                                'partner_id': partner.id,
                                'shopify_partner_id': shopify_customer.get('id'),
                                'shopify_instance_id': instance_id.id,
                            })
                        
                        # Exportar metafields del cliente
                        if partner.ref or partner.vat:
                            self._export_partner_metafields(partner, shopify_customer.get('id'), instance_id)
                            
                        _logger.info("WSSH Customer created/updated successfully: %s", partner.name)
                else:
                    _logger.error("WSSH Customer creation/update failed: %s", response.text if response else "No response")

            instance_id.last_export_customer = fields.Datetime.now()

    def _export_partner_metafields(self, partner, shopify_customer_id, instance_id):
        """Exporta los metafields de un partner a Shopify"""
        if not shopify_customer_id:
            return
            
        headers = {
            "X-Shopify-Access-Token": instance_id.shopify_shared_secret,
            "Content-Type": "application/json"
        }
        
        metafields = []
        
        # Añadir ref como metafield
        if partner.ref and not partner.ref.startswith('SID'):
            metafields.append({
                "namespace": "custom",
                "key": "ref",
                "value": partner.ref,
                "type": "number_integer" if partner.ref.isdigit() else "single_line_text_field"
            })
            
        # Añadir vat como metafield
        if partner.vat:
            metafields.append({
                "namespace": "custom",
                "key": "vat",
                "value": partner.vat,
                "type": "single_line_text_field"
            })
            
        # Exportar metafields
        for metafield in metafields:
            url = self.get_customer_url(instance_id, endpoint=f'customers/{shopify_customer_id}/metafields.json')
            data = {"metafield": metafield}
            
            response = requests.post(url, headers=headers, data=json.dumps(data))
            if not response or not response.ok:
                _logger.error("WSSH Metafield export failed: %s - %s", metafield.get('key'), 
                              response.text if response else "No response")

    def action_open_export_customer_to_shopify(self):
        return {
            'name': _('Export Customers to Shopify'),
            'res_model': 'customer.export.instance',
            'type': 'ir.actions.act_window',
            'view_mode': 'form',
            'target': 'new',
            'context': {},
        }