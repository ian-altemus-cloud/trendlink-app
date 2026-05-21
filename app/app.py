from unicodedata import category

from flask import Flask, request, jsonify
import boto3
import requests
import os
import logging
from prometheus_client import Counter, Histogram, generate_latest
from aws_xray_sdk.core import xray_recorder
from aws_xray_sdk.ext.flask.middleware import XRayMiddleware

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

# X-Ray tracing
xray_recorder.configure(service='trendlink-prospect-agent')
XRayMiddleware(app, xray_recorder)

# Config from environment — injected by Vault sidecar
GOOGLE_PLACES_API_KEY = os.environ.get('GOOGLE_PLACES_API_KEY')
AWS_REGION = os.environ.get('AWS_REGION', 'us-east-1')
DYNAMODB_TABLE = os.environ.get('DYNAMODB_TABLE', 'trendlink-prospects')

# DynamoDB client
dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
table = dynamodb.Table(DYNAMODB_TABLE)

@app.route('/health')
def health():
    return jsonify({'status': 'healthy'}), 200

@app.route('/metrics')
def metrics():
    return generate_latest(), 200, {'Content-Type': 'text/plain'}

@app.route('/search', methods=['POST'])
def search():
    with REQUEST_LATENCY.labels(endpoint='/search').time():
        data = request.get_json()
        category = data.get('category')
        location = data.get('location')

        if not category or not location:
            REQUEST_COUNT.labels(
                endpoint='/search',
                method='POST',
                status_code=400
            ).inc()
            return jsonify({'error': 'category and location are required'}), 400

        # Google Places API call
        url = 'https://maps.googleapis.com/maps/api/place/textsearch/json'
        params = {
            'query': f'{category} in {location}',
            'key': GOOGLE_PLACES_API_KEY,
        }

        response = requests.get(url, params=params)
        places = response.json().get('results')

        prospects = []
        for place in places[10]:
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
            status_code=200
        ).inc()

        return jsonify({'prospects': prospects}), 200

@app.route('/enrich', methods=['POST'])
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

        #Store in DynamoDB
        table.put_item(Item={
            'place_id': prospect.get('place_id'),
            'name': prospect.get('name'),
            'address': prospect.get('address'),
            'instagram': prospect.get('instagram', 'pending'),
            'enriched': False
        })

        REQUEST_COUNT.labels(
            endpoint='/enrich',
            method='POST',
            status='200'
        ).inc()

        return jsonify({'status': 'stored', 'prospect': prospect}), 200

@app.route('/prospects', methods=['GET'])
def get_prospects():
    with REQUEST_LATENCY.labels(endpoint='/prospects').time():
        response = table.scan()
        prospects = response.get('Items', [])

        REQUEST_COUNT.labels(
            endpoint='/prospects',
            method='GET',
            status_code='200'
        ).inc()

        return jsonify({'prospects': prospects}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)