#!/usr/bin/env python
"""Validation script for FastAPI home page HTML"""

from fastapi.testclient import TestClient
from app.main import app
import re

client = TestClient(app)
response = client.get('/')

# Check if response is successful
if response.status_code == 200:
    html = response.text
    
    # Look for flow-panel elements with aria-hidden="true" and style="display: none;"
    # Pattern: elements with class containing 'flow-panel' AND aria-hidden="true" AND style="display: none;"
    flow_panels = re.findall(
        r'<[^>]*flow-panel[^>]*aria-hidden=["\']true["\'][^>]*style=["\']display:\s*none[^>]*["\']|'
        r'<[^>]*aria-hidden=["\']true["\'][^>]*flow-panel[^>]*style=["\']display:\s*none[^>]*["\']|'
        r'<[^>]*style=["\']display:\s*none[^>]*["\'][^>]*flow-panel[^>]*aria-hidden=["\']true["\']',
        html
    )
    
    print("VALIDATION RESULT:")
    print("-" * 60)
    print(f"Response Status Code: {response.status_code}")
    print(f"Hidden flow-panel elements found: {len(flow_panels)}")
    
    if len(flow_panels) >= 2:
        print("✓ PASS - Both flow panels are hidden with aria-hidden='true' and style='display: none;'")
        print("\nHidden flow panels:")
        for i, panel in enumerate(flow_panels[:2], 1):
            print(f"  Panel {i}: {panel[:100]}...")
    else:
        print(f"✗ FAIL - Expected at least 2 hidden flow panels, found {len(flow_panels)}")
        
        # Debug info
        if 'flow-panel' in html:
            print("\nFlow-panel elements found in HTML (searching for them)...")
            panels = re.findall(r'<[^>]*flow-panel[^>]*>', html)
            print(f"Total flow-panel elements: {len(panels)}")
            print("Sample flow-panel elements:")
            for i, panel in enumerate(panels[:2], 1):
                print(f"  {i}. {panel}")
        else:
            print("\nNo 'flow-panel' found in HTML")
            
else:
    print(f"VALIDATION FAILED: Status code {response.status_code}")
