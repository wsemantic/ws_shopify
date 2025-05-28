# inherit res.partner model with fields to store shopify customer id and shopify instance id
import json
import re
import requests
import time
import traceback
from odoo import api, fields, models, _
from odoo.exceptions import UserError,ValidationError, UserError, AccessError
from odoo.tools import config
from psycopg2 import Error as PostgresError


import logging

_logger = logging.getLogger(__name__)

class ResPartner(models.Model):
    _inherit = 'res.partner'

    shopify_partner_map_ids = fields.One2many(
        'shopify.partner.map',
        'partner_id',
        string='Shopify Mappings'
    )
    

    def import_shopify_customers(self, shopify_instance_ids, skip_existing_customer):
        # Verificar que no hay problemas con el contexto
        _logger.info(f"WSSH Context active_ids: {self._context.get('active_ids', 'No definido')}")
        _logger.info(f"WSSH Context active_model: {self._context.get('active_model', 'No definido')}")
        
        # Configuración de timeouts y límites
        tout_medio = 300  # Timeout medio para requests individuales
        pagina_size = 100  # Tamaño de página para monitorización frecuente
        margen_seguridad = 60  # Margen de seguridad en segundos
        
        # Debug de variables
        _logger.info(f"WSSH Variables definidas: tout_medio={tout_medio}, pagina_size={pagina_size}, margen={margen_seguridad}")
        
        # Debug completo de la configuración
        _logger.info(f"WSSH === DEBUG CONFIGURACIÓN ===")
        limit_time_real_raw = config.get('limit_time_real')  # Sin default para ver el valor real
        _logger.info(f"WSSH limit_time_real RAW (sin default): '{limit_time_real_raw}' (tipo: {type(limit_time_real_raw)})")
        
        limit_time_real_with_default = config.get('limit_time_real', 120)
        _logger.info(f"WSSH limit_time_real con default: '{limit_time_real_with_default}' (tipo: {type(limit_time_real_with_default)})")
        
        # Convertir SIEMPRE a entero, sin importar el tipo original
        try:
            # Primero obtener el valor (string desde config o default int)
            raw_value = config.get('limit_time_real')
            
            if raw_value is None:
                # No está configurado, usar default
                limit_time_real = tout_medio  # Default más conservador
                _logger.info(f"WSSH limit_time_real no configurado, usando default: {limit_time_real}s (tout_medio={tout_medio})")
            else:
                # Está configurado, convertir a int
                limit_time_real = int(str(raw_value).strip())
                _logger.info(f"WSSH limit_time_real desde config: {limit_time_real}s")
                
            # Validar que sea un valor razonable (entre 60s y 2 horas)
            if limit_time_real < 60:
                _logger.warning(f"WSSH limit_time_real muy bajo: {limit_time_real}s, usando {tout_medio * 2}s")
                limit_time_real = tout_medio
                _logger.info(f"WSSH Valor corregido: limit_time_real={limit_time_real}s")
            elif limit_time_real > 7200:
                _logger.warning(f"WSSH limit_time_real muy alto: {limit_time_real}s, usando {tout_medio * 2}s")  
                limit_time_real = tout_medio * 2
                _logger.info(f"WSSH Valor corregido: limit_time_real={limit_time_real}s")  # 600s, no 60s
                
        except (ValueError, TypeError) as e:
            _logger.error(f"WSSH Error procesando limit_time_real '{raw_value}': {e}, usando {tout_medio * 2}s")
            limit_time_real = tout_medio * 2
        
        max_execution_time = limit_time_real - margen_seguridad  # Margen de seguridad
        
        # Asegurar que el timeout sea positivo
        if max_execution_time <= 0:
            _logger.warning(f"WSSH Timeout calculado negativo o cero ({max_execution_time}s), usando mínimo de 120s")
            max_execution_time = 120
        
        start_time = time.time()
        _logger.info(f"WSSH Timeout final calculado: {max_execution_time}s (desde limit_time_real: {limit_time_real}s)")
        _logger.info(f"WSSH Configuración final: página_size={pagina_size}, tout_medio={tout_medio}s, margen={margen_seguridad}s")
        _logger.info(f"WSSH === FIN DEBUG CONFIGURACIÓN ===")
        
        if not shopify_instance_ids:
            shopify_instance_ids = self.env['shopify.web'].sudo().search([('shopify_active', '=', True)])

        # Acumular todos los IDs de partners procesados
        all_customer_ids = []

        for shopify_instance_id in shopify_instance_ids:
            base_url = self.get_customer_url(shopify_instance_id, endpoint='customers.json')
            access_token = shopify_instance_id.shopify_shared_secret
            headers = {"X-Shopify-Access-Token": access_token}

            params = {"limit": pagina_size}

            if shopify_instance_id.shopify_last_date_customer_import:
                params["created_at_min"] = shopify_instance_id.shopify_last_date_customer_import

            # Usar since_id: último ID procesado o 0 para comenzar desde el inicio
            params["since_id"] = shopify_instance_id.shopify_last_import_customer_id or 0
            _logger.info(f"WSSH Consulta con since_id: {params['since_id']} ({'continuando' if shopify_instance_id.shopify_last_import_customer_id else 'desde inicio'})")

            url = base_url
            import_complete = False
            last_customer_id = None

            while True:
                # Verificar si se está acercando al timeout
                elapsed_time = time.time() - start_time
                if elapsed_time > max_execution_time:
                    _logger.warning(f"WSSH Tiempo límite alcanzado ({elapsed_time:.1f}s de {max_execution_time}s). Guardando progreso...")
                    if last_customer_id:
                        # Guardar progreso - si falla, que aborte
                        shopify_instance_id.write_with_retry(shopify_instance_id, 'shopify_last_import_customer_id', str(last_customer_id))
                        _logger.info(f"WSSH Progreso guardado. Último ID: {last_customer_id}")
                    return all_customer_ids  # Retornar lista de IDs de partners procesados

                try:
                    _logger.info(f"WSSH Captura clientes página {url}")
                    _logger.info(f"WSSH Parámetros enviados: {params}")
                    
                    response = requests.get(url, headers=headers, params=params, timeout=tout_medio)
                    response.raise_for_status()
                    shopify_customers = response.json()
                    customers = shopify_customers.get('customers', [])

                    if not customers:
                        import_complete = True
                        break

                    # Debug: mostrar IDs tal como vienen de la API
                    customer_ids = [customer.get('id') for customer in customers]
                    _logger.info(f"WSSH IDs recibidos (página {pagina_size}): {customer_ids[:3]}...{customer_ids[-3:] if len(customer_ids) > 3 else customer_ids}")
                    _logger.info(f"WSSH Total clientes en página: {len(customers)} - Rango: {customers[0]['id']} a {customers[-1]['id']}")

                    for customer in customers:
                        customer['metafields'] = self.get_customer_metafields(customer.get('id'), shopify_instance_id)

                    all_customer_ids = self.create_customers(customers, shopify_instance_id, skip_existing_customer)
                    
                    # Para IDs ascendentes, siempre guardamos el último (máximo) para usar en since_id
                    last_customer_id = customers[-1]['id']
                    _logger.info(f"WSSH Página procesada en {time.time() - start_time:.1f}s. Último ID en memoria: {last_customer_id}")
                    
                    # NO guardar progreso en cada página - solo trackear en memoria

                    link_header = response.headers.get('Link')
                    if link_header:
                        links = shopify_instance_id._parse_link_header(link_header)
                        if 'next' in links:
                            url = links['next']
                            params = None
                            # Delay para respetar rate limits de Shopify
                            _logger.info("WSSH Esperando 2s antes de siguiente página para respetar rate limits")
                            time.sleep(2)
                            continue

                    import_complete = True
                    break

                except requests.exceptions.HTTPError as e:
                    if e.response and e.response.status_code == 429:
                        # Rate limit específico de Shopify
                        retry_after = int(e.response.headers.get('Retry-After', 10))
                        _logger.warning(f"WSSH Rate limit alcanzado. Esperando {retry_after}s antes de reintentar...")
                        time.sleep(retry_after)
                        # Reintentar la misma página
                        continue
                    else:
                        # Otros errores HTTP - manejar como antes
                        tb_str = traceback.format_exc()
                        _logger.error(f"WSSH Traceback completo:\n{tb_str}")
                        
                        _logger.warning("Error during customer import (Sin rollback). Last customer ID: %s. Error: %s", 
                                      last_customer_id or 'N/A', str(e))
                        
                        if last_customer_id:
                            try:
                                shopify_instance_id.write_with_retry(shopify_instance_id, 'shopify_last_import_customer_id', str(last_customer_id))
                                _logger.info(f"WSSH Progreso guardado tras error HTTP. Último ID: {last_customer_id}")
                            except Exception as save_error:
                                _logger.error(f"WSSH Error guardando progreso: {save_error}")
                        break
                    # Obtener traceback completo para debugging
                    tb_str = traceback.format_exc()
                    _logger.error(f"WSSH Traceback completo:\n{tb_str}")
                    
                    # Determinar si la excepción permite guardar progreso (no causa rollback)
                    save_progress_allowed = False
                    
                    # Excepciones que NO causan rollback - seguro guardar progreso
                    if isinstance(e, (requests.exceptions.RequestException, 
                                    requests.exceptions.Timeout,
                                    requests.exceptions.ConnectionError,
                                    ValueError, TypeError, KeyError,
                                    UnicodeError, AttributeError)) and not isinstance(e, (PostgresError, ValidationError, UserError, AccessError)):
                        save_progress_allowed = True
                        error_type = "Sin rollback"
                    else:
                        # Excepciones de BD (PostgreSQL), validación de Odoo, etc. - NO guardar progreso
                        error_type = "Con rollback"
                        if isinstance(e, PostgresError):
                            _logger.error(f"WSSH Error PostgreSQL detectado: {type(e).__name__}")
                        elif isinstance(e, (ValidationError, UserError, AccessError)):
                            _logger.error(f"WSSH Error Odoo detectado: {type(e).__name__}")
                    
                    _logger.warning("Error during customer import (%s). Last customer ID: %s. Error: %s", 
                                  error_type, last_customer_id or 'N/A', str(e))
                    
                    # Solo guardar progreso si la excepción no causa rollback
                    if save_progress_allowed and last_customer_id:
                        try:
                            shopify_instance_id.write_with_retry(shopify_instance_id, 'shopify_last_import_customer_id', str(last_customer_id))
                            _logger.info(f"WSSH Progreso guardado tras error sin rollback. Último ID: {last_customer_id}")
                        except Exception as save_error:
                            _logger.error(f"WSSH Error guardando progreso: {save_error}")
                    elif last_customer_id:
                        _logger.warning(f"WSSH Progreso NO guardado - excepción causa rollback")
                    
                    break

            if import_complete:
                # Resetear ID de último import y actualizar fecha - si falla, que aborte
                shopify_instance_id.write_with_retry(shopify_instance_id, 'shopify_last_import_customer_id', False)
                shopify_instance_id.write_with_retry(shopify_instance_id, 'shopify_last_date_customer_import', fields.Datetime.now())
                _logger.info(f"WSSH Importación completada exitosamente en {time.time() - start_time:.1f}s")

        return all_customer_ids


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
        
        # Usar el nuevo método para procesar la dirección base
        address_vals = self.prepare_address_vals(first_address, shopify_instance_id, 'invoice')
                                                                             
        first_name = shopify_customer.get('first_name') or first_address.get('first_name') or ''
        last_name = shopify_customer.get('last_name') or first_address.get('last_name') or ''
        email = shopify_customer.get('email') or first_address.get('email') or ''
        phone = shopify_customer.get('phone') or first_address.get('phone') or ''
        name = self._get_customer_name(first_name, last_name, email)                                                                        

        # Combinar datos del cliente con los de la dirección
        vals = address_vals.copy()
        vals.update({
            'name': name,
            'customer_rank': 1,
            'email': email,
            'phone': phone,
            'ref': metafields.get('ref') or ('SID' + str(shopify_customer.get('id'))),
            'user_id': shopify_instance_id.salesperson_id.id if shopify_instance_id.salesperson_id else False,  # Asignar comercial
        })
        
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
        name = first_name
        if first_name and last_name:
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
        
# Añadir estos métodos a la clase ResPartner en res_partner.py

    def prepare_address_vals(self, address_data, shopify_instance_id, address_type='contact'):
        """Prepara los valores para crear o actualizar una dirección específica."""
        if not address_data:
            return {}
            
        first_name = address_data.get('first_name', '')
        last_name = address_data.get('last_name', '')
        email = address_data.get('email', '')
        phone = address_data.get('phone', '')
        name = self._get_customer_name(first_name, last_name, email)
        
        street = address_data.get('address1')
        street2 = address_data.get('address2')
        city = address_data.get('city')
        zip = address_data.get('zip')
        country_code = address_data.get('country_code')
        country_id = self.env['res.country'].sudo().search([('code', '=', country_code)], limit=1).id if country_code else False
        
        vals = {
            'name': name,
            'street': street,
            'street2': street2,
            'city': city,
            'zip': zip,
            'phone': phone,
            'country_id': country_id,
            'is_company': False,
            'supplier_rank': 0,
            'customer_rank': 1 if address_type == 'invoice' else 0,
        }
        
        return vals

    def get_or_create_shipping_partner(self, shipping_address, parent_partner, shopify_instance_id):
        """Busca o crea un partner hijo para dirección de envío."""
        if not shipping_address:
            return parent_partner.id
            
        # Buscar partner hijo existente con los mismos datos
        address_vals = self.prepare_address_vals(shipping_address, shopify_instance_id, 'delivery')
        
        # Buscar por dirección similar en los hijos del partner
        domain = [
            ('parent_id', '=', parent_partner.id),
            ('type', '=', 'delivery')
        ]
        
        # Añadir filtros por campos clave si existen
        if address_vals.get('street'):
            domain.append(('street', '=', address_vals['street']))
        if address_vals.get('city'):
            domain.append(('city', '=', address_vals['city']))
        if address_vals.get('zip'):
            domain.append(('zip', '=', address_vals['zip']))
            
        existing_shipping_partner = self.search(domain, limit=1)
        
        if existing_shipping_partner:
            return existing_shipping_partner.id
        else:
            # Crear nuevo partner hijo para dirección de envío
            address_vals.update({
                'parent_id': parent_partner.id,
                'type': 'delivery',
                'customer_rank': 0,
            })
            shipping_partner = self.create(address_vals)
            _logger.info(f"WSSH Creado partner de envío: {shipping_partner.name} para {parent_partner.name}")
            return shipping_partner.id

    def addresses_are_different(self, shipping_address, billing_address):
        """Compara si dos direcciones son diferentes."""
        if not shipping_address or not billing_address:
            return bool(shipping_address)  # Diferentes si solo una existe
            
        # Campos clave para comparar
        key_fields = ['address1', 'address2', 'city', 'zip', 'country_code', 'province_code']
        
        for field in key_fields:
            ship_val = (shipping_address.get(field) or '').strip()
            bill_val = (billing_address.get(field) or '').strip()
            if ship_val != bill_val:
                return True
                
        return False        