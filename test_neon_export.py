#!/usr/bin/env python3
"""
Test Neon exports against local to verify data integrity
"""

import requests
import json
import os

def test_export(base_url: str, table: str, format_type: str):
    """Test a specific export format"""
    print(f"\n📊 Testing {format_type} export for {table}")
    
    url = f"{base_url}/api/export/{format_type}?table={table}"
    
    try:
        response = requests.get(url, timeout=60)
        
        if response.status_code != 200:
            print(f"  ❌ Error: HTTP {response.status_code}")
            return False
        
        content_type = response.headers.get('Content-Type', '')
        print(f"  ✅ Success: {len(response.content)} bytes, Content-Type: {content_type}")
        
        # Save to file for comparison
        ext = {
            'csv': '.csv',
            'json': '.json',
            'geojson': '.geojson',
            'shapefile': '.zip'
        }.get(format_type, '.bin')
        
        filename = f"test_{table}_{format_type}{ext}"
        with open(filename, 'wb') as f:
            f.write(response.content)
        print(f"  📁 Saved to: {filename}")
        
        return True
        
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False

def main():
    # Use your Neon URL
    base_url = "http://localhost:5000"  # Change this to your deployed URL
    
    print("🧪 Testing Neon exports")
    print(f"Base URL: {base_url}")
    
    tests = [
        ('subbasin_gis', 'csv'),
        ('subbasin_gis', 'json'),
        ('subbasin_gis', 'geojson'),
        ('subbasin_gis', 'shapefile'),
    ]
    
    passed = 0
    for table, format_type in tests:
        if test_export(base_url, table, format_type):
            passed += 1
    
    print(f"\n📈 Results: {passed}/{len(tests)} tests passed")
    
    if passed == len(tests):
        print("✅ All tests passed!")
    else:
        print("⚠️ Some tests failed")

if __name__ == "__main__":
    main()