"""
update_secrets.py
Reads local secrets/secrets.json and injects DB URLs and API keys into the Master DB.
"""
import json
import os
import sys
import psycopg2

def run_update_secrets(secrets_file="secrets/secrets.json"):
    filepath = os.path.join(os.path.dirname(__file__), secrets_file)
    
    if not os.path.exists(filepath):
        print(f"Error: {filepath} not found.")
        return

    with open(filepath, "r", encoding="utf-8") as f:
        secrets = json.load(f)

    # 1. Connect to Master DB using the root key
    master_db_url = secrets.get("MASTER_DATABASE_URL")
    if not master_db_url:
        print("Error: MASTER_DATABASE_URL missing in secrets.json")
        return

    try:
        conn = psycopg2.connect(master_db_url, sslmode="require")
        conn.autocommit = True
        cur = conn.cursor()

        # 2. Loop through Clients
        clients = secrets.get("clients", {})
        for client_id, data in clients.items():
            client_db = data.get("db_connect_string")
            client_key = data.get("gemini_api_key")
            
            # Update the client record
            if client_db and client_key:
                cur.execute(
                    "UPDATE clients SET db_connect_string = %s, gemini_api_key = %s WHERE client_id = %s", 
                    (client_db, client_key, client_id)
                )
            elif client_db:
                cur.execute("UPDATE clients SET db_connect_string = %s WHERE client_id = %s", (client_db, client_id))
            elif client_key:
                cur.execute("UPDATE clients SET gemini_api_key = %s WHERE client_id = %s", (client_key, client_id))
                
            print(f"Updated Client {data.get('client_name', client_id[:8])}...")

            # 3. Loop through Instances nested under this Client
            instances = data.get("instances", {})
            for instance_id, inst_data in instances.items():
                inst_db = inst_data.get("db_connect_string")
                inst_key = inst_data.get("gemini_api_key")
                
                # Note: We set to NULL if they are null in JSON, allowing inheritance from the client
                cur.execute(
                    "UPDATE instances SET db_connect_string = %s, gemini_api_key = %s WHERE instance_id = %s", 
                    (inst_db, inst_key, instance_id)
                )
                print(f"  -> Updated Instance {instance_id[:8]}...")

        print("\nSecrets successfully injected into the database.")
        cur.close()
        conn.close()

    except Exception as e:
        print(f"Database error: {e}")

if __name__ == "__main__":
    run_update_secrets()