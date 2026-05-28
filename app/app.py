from flask import Flask, request, jsonify
import boto3
import requests
import os
import logging
from prometheus_client import Counter, Histogram, generate_latest


app = Flask(__name__)

# Prometheus metrics
REQUEST_COUNT = Counter(
    'prospect_agent_requests_total',
    'Total requests',
    ['endpoint', 'method', 'status']
)

REQUEST_LATENCY = Histogram(
    'prospect_agent_requests_latency_seconds',
    'Request latency',
    ['endpoint']
)

# --- SECRET RESOLUTION --
def get_google_api_key():
    try:
        with open('/vault/secrets/google', 'r') as f:
            content = f.read()
        import re
        match = re.search(r'api_key:([^\s\]]+)', content)
        if match:
            return match.group(1).strip()
    except FileNotFoundError:
        pass
    return os.environ.get('GOOGLE_PLACES_API_KEY', '')

# Get aws credentials from vault
def get_aws_credentials():
    try:
        with open('/vault/secrets/aws', 'r') as f:
            content = f.read()
        import re
        access_key = re.search(r'access_key_id:([^\s\]]+)', content)
        secret_key = re.search(r'secret_access_key:([^\s\]]+)', content)
        if access_key and secret_key:
            return access_key.group(1).strip(), secret_key.group(1).strip()
    except FileNotFoundError:
        pass
    return None, None

# Config from environment — injected by Vault sidecar
GOOGLE_PLACES_API_KEY = get_google_api_key()
AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
DYNAMODB_TABLE = os.environ.get('DYNAMODB_TABLE', 'trendlink-prospects')

# DynamoDB client
access_key, secret_key = get_aws_credentials()
dynamodb = boto3.resource(
    'dynamodb',
            region_name=AWS_REGION,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key
)
table = dynamodb.Table(DYNAMODB_TABLE)

@app.route('/prospect-agent/health')
def health():
    return jsonify({'status': 'healthy'}), 200

@app.route('/prospect-agent/metrics')
def metrics():
    return generate_latest(), 200, {'Content-Type': 'text/plain'}

@app.route('/prospect-agent/search', methods=['POST'])
def search():
    with REQUEST_LATENCY.labels(endpoint='/search').time():
        data = request.get_json()
        category = data.get('category')
        location = data.get('location')

        if not category or not location:
            REQUEST_COUNT.labels(
                endpoint='/search',
                method='POST',
                status=400
            ).inc()
            return jsonify({'error': 'category and location are required'}), 400

        # Google Places API call
        url = 'https://maps.googleapis.com/maps/api/place/textsearch/json'
        params = {
            'query': f'{category} in {location}',
            'key': GOOGLE_PLACES_API_KEY,
        }

        response = requests.get(url, params=params)
        places = response.json().get('results', [])

        prospects = []
        for place in places[:10]:
            prospect = {
                'name': place.get('name'),
                'address': place.get('formatted_address'),
                'place_id': place.get('place_id'),
                'rating': place.get('rating'),
                'phone': place.get('phone'),
                'website': place.get('website'),
                'instagram': place.get('instagram'),

            }
            prospects.append(prospect)

        REQUEST_COUNT.labels(
            endpoint='/search',
            method='POST',
            status=200
        ).inc()

        return jsonify({'prospects': prospects}), 200

@app.route('/prospect-agent/enrich', methods=['POST'])
def enrich():
    with REQUEST_LATENCY.labels(endpoint='/enrich').time():
        data = request.get_json()
        prospect = data.get('prospect')

        if not prospect:
            REQUEST_COUNT.labels(
                endpoint='/enrich',
                method='POST',
                status='400'
            ).inc()
            return jsonify({'error': 'prospect is required'}), 400

        place_id = prospect.get('place_id')

        # Call Google Places Details API
        details_url = 'https://maps.googleapis.com/maps/api/place/details/json'
        params = {
            'place_id': place_id,
            'fields': 'name,formatted_phone_number,website,rating',
            'key': GOOGLE_PLACES_API_KEY,
        }
        details_response = requests.get(details_url, params=params)
        details = details_response.json().get('result', {})

        # Store enriched prospect in DynamoDB
        table.put_item(Item={
            'place_id': place_id,
            'name': prospect.get('name'),
            'address': prospect.get('address'),
            'phone': prospect.get('formatted_phone'),
            'website': details.get('website'),
            'rating': str(details.get('rating', '')),
            'instagram': None
            'enriched': True
        })

        REQUEST_COUNT.labels(
            endpoint='/enrich',
            method='POST',

        ).inc()

        return jsonify({
            'status': 'enriched',
            'prospect': {
                'place_id': place_id,
                'name': prospect.get('name'),
                'address': prospect.get('address'),
                'phone': details.get('formatted_phone_number'),
                'website': details.get('website'),
                'rating': details.get('rating'),
                'instagram': None,
                'enriched': True
            }
        }), 200

@app.route('/prospect-agent/prospects', methods=['GET'])
def get_prospects():
    with REQUEST_LATENCY.labels(endpoint='/prospects').time():
        response = table.scan()
        prospects = response.get('Items', [])

        REQUEST_COUNT.labels(
            endpoint='/prospects',
            method='GET',
            status='200'
        ).inc()

        return jsonify({'prospects': prospects}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)