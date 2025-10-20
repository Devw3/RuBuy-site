import sqlite3
import requests
from bs4 import BeautifulSoup
from translate import Translator
import json
import re
from deep_translator import GoogleTranslator
from urllib.parse import urlparse, parse_qs
from functools import lru_cache

@lru_cache(maxsize=100)
def translate_text(text, source='zh-CN', target='ru'):
    try:
        if not text or text == 'Неизвестно':
            return text
        return GoogleTranslator(source=source, target=target).translate(text)
    except Exception as e:
        print(f"Ошибка перевода '{text}': {str(e)}")
        return text


# Парсинг информации о товаре с веидиан
def parse_weidian_product(url):
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Ошибка загрузки страницы: {response.status_code}")

    soup = BeautifulSoup(response.text, 'html.parser')
    script_tag = soup.find('script', {'id': '__rocker-render-inject__'})
    if not script_tag:
        raise Exception("Не удалось найти данные о товаре")

    json_data = script_tag.get('data-obj')
    if not json_data:
        raise Exception("Не удалось извлечь JSON-данные")

    data = json.loads(json_data)
    try:
        item_info = data['result']['default_model']['item_info']
        sku_properties = data['result']['default_model']['sku_properties']
        attr_list = sku_properties['attr_list']
        sku_data = sku_properties['sku']
    except (KeyError, TypeError):
        raise Exception("Некорректная структура JSON")
    
    title = item_info.get('item_name', 'Неизвестно')

    # Изменение здесь: переименовали price в base_price
    product = {
        'title': title,
        'base_price': item_info.get('itemLowPrice', 0) / 100,  
        'models': []
    }

    attr_maps = []
    for attr in attr_list:
        attr_values = {}
        for val in attr.get('attr_values', []):
            original_val = val['attr_value']
            translated_val = original_val
            attr_values[str(val['attr_id'])] = translated_val
        attr_maps.append(attr_values)
                         
    models = {}
    for sku in sku_data.values():
        attr_ids = sku['attr_ids'].split('-')
        if len(attr_ids) != len(attr_maps):
            continue

        color_id = attr_ids[0]
        size_id = attr_ids[1] if len(attr_ids) > 1 else None

        color_name = attr_maps[0].get(color_id, 'Неизвестный')
        size_name = attr_maps[1].get(size_id, 'Без размера') if size_id else 'Без размера'

        key = f"{color_id}"
        if key not in models:
            models[key] = {
                'color': color_name,
                'image': sku.get('img'),
                'sizes': []
            }

        models[key]['sizes'].append({
            'size': size_name,
            'price': float(sku.get('price', 0)),
            'stock': sku.get('stock', 0)
        })

    for model in models.values():
        for size in model['sizes']:
            product['models'].append({
                'color_name': model['color'],
                'size_name': size['size'],
                'price': size['price'],
                'stock': size['stock'],
                'image_url': model['image']
            })

    return product

# Добавление в базу данных
# def save_to_database(product):
#     conn = sqlite3.connect(DB_NAME)
#     cursor = conn.cursor()

#     # Добавим товар
#     cursor.execute('''
#         INSERT INTO products (title, base_price)
#         VALUES (?, ?)
#     ''', (product['title'], product['price']))
#     product_id = cursor.lastrowid

#     # Добавим модели
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
#     print(f"✅ Товар '{product['title']}' добавлен в базу данных.\n")


def new_product(url: str):
    try:
        product = parse_product_info_from_url(url)
        save_to_database(product, url)
        return True
    except Exception as e:
        print(f"❌ Не удалось добавить товар: {e}")
        return False

# # Основная точка входа
# if __name__ == "__main__":
#     while True:
#         url = input("Вставь ссылку на товар (или 'exit' для выхода): ").strip()
#         if url.lower() == 'exit':
#             break
#         try:
#             product = parse_product_info_from_url(url)
#             save_to_database(product)
#         except Exception as e:
#             print("❌ Ошибка:", e)
