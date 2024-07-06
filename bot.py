import os
import redis
from flask import Flask, request, jsonify
from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.resource import ResourceManagementClient
from dotenv import load_dotenv

# Cargar las variables de entorno desde el archivo .env
load_dotenv()

# Configurar las credenciales de Azure
credential = ClientSecretCredential(
    tenant_id=os.getenv('AZURE_TENANT_ID'),
    client_id=os.getenv('AZURE_CLIENT_ID'),
    client_secret=os.getenv('AZURE_CLIENT_SECRET')
)

subscription_id = os.getenv('AZURE_SUBSCRIPTION_ID')
compute_client = ComputeManagementClient(credential, subscription_id)
resource_client = ResourceManagementClient(credential, subscription_id)

# Configurar Redis (cambiar host a 'redis' que es el nombre del contenedor)
redis_client = redis.StrictRedis(
    host='redis',  # Nombre del contenedor de Redis
    port=6379,
    decode_responses=True
)

# Configurar Flask
app = Flask(__name__)

@app.route('/api/messages', methods=['POST'])
def messages():
    user_id = request.json['from']['id']
    message_text = request.json['text'].lower()

    state = redis_client.get(user_id)

    if state is None:
        if "crear vm" in message_text:
            redis_client.set(user_id, 'awaiting_resource_group')
            return jsonify({'message': '¿Quieres crear la VM en un nuevo grupo de recursos o en uno existente?'})
        else:
            return jsonify({'message': 'Comando no reconocido. Por favor, escribe "crear VM" para comenzar.'})

    elif state == 'awaiting_resource_group':
        if "nuevo" in message_text:
            redis_client.set(user_id, 'creating_resource_group')
            return jsonify({'message': 'Por favor, proporciona el nombre del nuevo grupo de recursos.'})
        elif "existente" in message_text:
            redis_client.set(user_id, 'awaiting_existing_resource_group')
            return jsonify({'message': 'Por favor, proporciona el nombre del grupo de recursos existente.'})
        else:
            return jsonify({'message': 'Por favor, responde con "nuevo" o "existente".'})

    elif state == 'creating_resource_group':
        resource_group_name = message_text
        redis_client.set(f'{user_id}_resource_group', resource_group_name)
        redis_client.set(user_id, 'creating_network')
        return jsonify({'message': f'Creando el grupo de recursos "{resource_group_name}". Ahora, por favor, proporciona un nombre para la red virtual.'})

    elif state == 'awaiting_existing_resource_group':
        resource_group_name = message_text
        redis_client.set(f'{user_id}_resource_group', resource_group_name)
        redis_client.set(user_id, 'creating_network')
        return jsonify({'message': f'Utilizando el grupo de recursos "{resource_group_name}". Ahora, por favor, proporciona un nombre para la red virtual.'})

    elif state == 'creating_network':
        network_name = message_text
        redis_client.set(f'{user_id}_network', network_name)
        redis_client.set(user_id, 'creating_vm')
        return jsonify({'message': f'Creando la red virtual "{network_name}". Ahora, por favor, proporciona un nombre para la máquina virtual.'})

    elif state == 'creating_vm':
        vm_name = message_text
        resource_group_name = redis_client.get(f'{user_id}_resource_group')
        network_name = redis_client.get(f'{user_id}_network')
        try:
            create_resources(resource_group_name, network_name, vm_name)
            redis_client.delete(user_id)
            redis_client.delete(f'{user_id}_resource_group')
            redis_client.delete(f'{user_id}_network')
            return jsonify({'message': f'La máquina virtual "{vm_name}" se ha creado correctamente en el grupo de recursos "{resource_group_name}" con la red "{network_name}".'})
        except Exception as e:
            return jsonify({'message': f'Error al crear la máquina virtual: {str(e)}'}), 500

    return jsonify({'message': 'Comando no reconocido.'})

def create_resources(resource_group_name, network_name, vm_name):
    # Crear el grupo de recursos
    resource_client.resource_groups.create_or_update(resource_group_name, {'location': 'eastus'})

    # Crear la red virtual
    network_parameters = {
        'location': 'eastus',
        'address_space': {'address_prefixes': ['10.0.0.0/16']}
    }
    network_client = resource_client.network_client.virtual_networks
    network_client.begin_create_or_update(resource_group_name, network_name, network_parameters).result()

    # Crear la interfaz de red
    subnet = network_client.subnets.get(resource_group_name, network_name, 'default')
    nic_parameters = {
        'location': 'eastus',
        'ip_configurations': [{
            'name': 'default',
            'subnet': {'id': subnet.id},
            'private_ip_allocation_method': 'Dynamic'
        }]
    }
    nic_client = resource_client.network_client.network_interfaces
    nic = nic_client.begin_create_or_update(resource_group_name, f'{vm_name}-nic', nic_parameters).result()

    # Crear la máquina virtual
    vm_parameters = {
        'location': 'eastus',
        'hardware_profile': {'vm_size': 'Standard_DS1_v2'},
        'storage_profile': {
            'image_reference': {
                'publisher': 'Canonical',
                'offer': 'UbuntuServer',
                'sku': '18.04-LTS',
                'version': 'latest'
            },
            'os_disk': {
                'name': 'osdisk',
                'create_option': 'FromImage'
            }
        },
        'os_profile': {
            'computer_name': vm_name,
            'admin_username': 'azureuser',
            'admin_password': 'Password123!'
        },
        'network_profile': {'network_interfaces': [{'id': nic.id}]}
    }
    compute_client.virtual_machines.begin_create_or_update(resource_group_name, vm_name, vm_parameters).result()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=3978)
