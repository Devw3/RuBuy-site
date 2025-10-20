import sqlite3
import requests
import re
from urllib.parse import urlparse, parse_qs

def extract_item_id(url):
    id_match = re.search(r'(?:id=|item_id=)(\d+)', url)
    if id_match:
        return id_match.group(1)

    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    if 'id' in params:
        return params['id'][0]
    return None


def parse_taobao_product(api_token, product_url):
    item_id = extract_item_id(product_url)
    if not item_id:
        raise Exception("Не удалось извлечь ID товара из URL")

    url = "http://api.tmapi.top/taobao/item_detail"
    params = {"apiToken": api_token, "item_id": item_id}
    headers = {"User-Agent": "Mozilla/5.0"}

    response = requests.get(url, headers=headers, params=params)
    response.raise_for_status()
    data = response.json()

    if data['code'] != 200:
        raise Exception(f"API error: {data['msg']}")

    return process_product_data(data['data'])


def process_product_data(product_data):
    product_info = {
        'title': product_data.get('title', 'Неизвестно'),
        'base_price': float(product_data.get('price_info', {}).get('price', 0)),
        'models': []
    }

    sku_props = product_data.get('sku_props', [])
    skus = product_data.get('skus', [])

    color_prop = next((p for p in sku_props if p.get('prop_name') in ['颜色分类', '颜色']), None)
    size_prop = next((p for p in sku_props if p.get('prop_name') in ['鞋码', '尺码', '尺寸']), None)

    color_map = {v['vid']: {'name': v['name'], 'image': v.get('imageUrl')}
                 for v in color_prop.get('values', [])} if color_prop else {}
    size_map = {v['vid']: v['name'] for v in size_prop.get('values', [])} if size_prop else {}

    for sku in skus:
        props_ids = sku.get('props_ids', '')
        color_id = None
        size_id = None

        parts = props_ids.split(';')
        for part in parts:
            if ':' not in part:
                continue
            pid, vid = part.split(':')
            if vid in color_map:
                color_id = vid
            elif vid in size_map:
                size_id = vid

        color_name = color_map.get(color_id, {}).get('name', 'Без названия')
        size_name = size_map.get(size_id, 'Без размера')
        image_url = color_map.get(color_id, {}).get('image')

        if ':' in size_name:
            size_name = size_name.split(':')[-1].strip()

        product_info['models'].append({
            'color_name': color_name,
            'size_name': size_name,
            'price': float(sku.get('sale_price', 0)),
            'stock': sku.get('stock', 0),
            'image_url': image_url
        })

    return product_info


# def save_to_database(product):
#     conn = sqlite3.connect(DB_NAME)
#     cursor = conn.cursor()

#     cursor.execute('''
#         INSERT INTO products (title, base_price)
#         VALUES (?, ?)
#     ''', (product['title'], product['price']))
#     product_id = cursor.lastrowid

#     for model in product['models']:
#         cursor.execute('''
#             INSERT INTO models (product_id, color_name, size_name, price, stock, image_url)
#             VALUES (?, ?, ?, ?, ?, ?)
#         ''', (
#             product_id,
#             model['color_name'],
#             model['size_name'],
#             model['price'],
#             model['stock'],
#             model['image_url']
#         ))

#     conn.commit()
#     conn.close()
#     print(f"✅ Товар '{product['title']}' добавлен в базу данных. Моделей: {len(product['models'])}\n")

