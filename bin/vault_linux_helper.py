import mysql.connector
import subprocess
import os
import logging
from sqlalchemy import create_engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class VaultLinuxManager:
    def __init__(self):
        self.vault_addr = "http://127.0.0.1:8200" 
        self.current_dir = os.path.dirname(os.path.abspath(__file__))
        self.token_file = os.path.join(self.current_dir, '.vault_remote_token')
        #self.token_file = os.path.join(os.path.expanduser('/home/max/vault_linux/'), '.vault_remote_token')

    def _get_vault_field(self, secret_path, field):
        """Intenta obtener la credencial desde Vault CLI instalado en Linux"""
        try:
            if not os.path.exists(self.token_file):
                return None
            
            with open(self.token_file, 'r') as f:
                token = f.read().strip()

            env = os.environ.copy()
            env['VAULT_ADDR'] = self.vault_addr
            env['VAULT_TOKEN'] = token

            result = subprocess.run(
                ['vault', 'kv', 'get', f'-field={field}', f'secret/{secret_path}'],
                capture_output=True,
                text=True,
                env=env
            )
            
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except Exception as e:
            logger.warning(f"No se pudo conectar a Vault: {e}")
            return None

    def get_linux_mysql_connection(self, database=None, force_local=True, server='prime'):
        username = self._get_vault_field('mysql/clasico-local', 'username')
        password = self._get_vault_field('mysql/clasico-local', 'password')
        host     = self._get_vault_field('mysql/clasico-local', 'host')
        port     = self._get_vault_field('mysql/clasico-local', 'port')

        if force_local:
            print("Conectando a LOCALHOST")
            username, password, host, port = 'root', '@_Clasic0Root2025DB_M8qP3nP12', '10.0.0.69', 3306
        
        elif not (username and password and host and port):
            print("Usando credenciales de respaldo de PRODUCCION")
            if server == 'clasico':
                username, password, host, port = 'root', '@_Clasic0Root2025DB_M8qP3nP12', '10.0.0.69', 3306
            else:
                username, password, host, port = 'root', '@_SecureRoot2025DB_M8qP3nX7', '10.0.0.68', 8806

        puerto_final = str(port)

        params = {
            'user': username,
            'password': password,
            'host': host,
            'port': puerto_final,
            'charset': 'utf8mb4',
            'collation': 'utf8mb4_unicode_ci',
            'autocommit': True if force_local else False
        }

        if database:
            params['database'] = database
            
        return mysql.connector.connect(**params)
    
    def get_sqlalchemy_engine(self, database):
        from sqlalchemy import create_engine
        try:
            from urllib.parse import quote_plus
        except ImportError:
            from urllib import quote_plus

        u = self._get_vault_field('mysql/clasico-local', 'username')
        p = self._get_vault_field('mysql/clasico-local', 'password')
        h = self._get_vault_field('mysql/clasico-local', 'host')
        pt = self._get_vault_field('mysql/clasico-local', 'port')

        if not u or not p:
            print("--- Usando credenciales de respaldo para la conexion ---")
            u = 'root'
            p = '@_Clasic0Root2025DB_M8qP3nP12'
            h = '10.0.0.69'
            pt = '3306'
        url = "mysql+mysqlconnector://{0}:{1}@{2}:{3}/{4}?charset=utf8mb4".format(
            str(u), 
            quote_plus(str(p)), 
            str(h), 
            str(pt), 
            str(database)
        )
        
        return create_engine(url)
_manager = VaultLinuxManager()

def get_engine(database):
    return _manager.get_sqlalchemy_engine(database)
