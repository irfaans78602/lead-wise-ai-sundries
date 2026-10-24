"""
sign_ask_ai.py
Generates a signed script tag for a client to embed the Ask AI widget.

Usage:
    python sign_ask_ai.py <instance_id>
    
Example:
    python sign_ask_ai.py 123e4567-e89b-12d3-a456-426614174000
"""

import sys
import os
from security import sign_combined_id

def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
        
    instance_id = sys.argv[1]
    
    # Ensure the keys are loaded
    if not os.environ.get("LINK_SIGNING_KEYS"):
        print("ERROR: LINK_SIGNING_KEYS environment variable is not set.")
        sys.exit(1)

    try:
        kid, sig = sign_combined_id(instance_id)
    except Exception as e:
        print(f"Error signing instance_id: {e}")
        sys.exit(1)

    print("\n--- Ask AI Script Tag ---")
    print(f'<script src="https://lead-wise-ai.com/assets/js/ask-ai.min.js" ')
    print(f'        data-instance-id="{instance_id}" ')
    print(f'        data-sig="{sig}" ')
    print(f'        data-kid="{kid}" ')
    print(f'        defer></script>')
    print("-------------------------\n")

if __name__ == "__main__":
    main()