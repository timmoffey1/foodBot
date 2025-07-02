import logging
import aiohttp
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler
)
import firebase_admin
from firebase_admin import credentials, firestore
import easyocr
import cv2
import numpy as np
import io

import os
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

logging.basicConfig(level=logging.INFO)

# Firebase
cred = credentials.Certificate("firebase_key.json")
firebase_admin.initialize_app(cred)
db = firestore.client()

# EasyOCR
reader = easyocr.Reader(['en'])  # Языки для распознавания

# Состояния
CODE, QUALITY, REVIEW, CONFIRM_UPDATE = range(4)

# Клавиатуры
yes_no_keyboard = ReplyKeyboardMarkup([["Да", "Нет"]], one_time_keyboard=True, resize_keyboard=True)
quality_keyboard = ReplyKeyboardMarkup([["1", "2", "3", "4", "5"]], one_time_keyboard=True, resize_keyboard=True)

# Команда /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привет! Отправь фото с штрихкодом или введи его вручную:")
    return CODE

# Получение инфо из OpenFoodFacts
async def fetch_product_info(barcode: str):
    url = f"https://world.openfoodfacts.org/api/v0/product/{barcode}.json"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("status") == 1:
                    product = data.get("product", {})
                    name = product.get("product_name", "Без названия")
                    brands = product.get("brands", "")
                    return {"name": name, "brands": brands}
            return None

# Обработка фото или текста
async def code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message

    if message.photo:
        photo_file = await message.photo[-1].get_file()
        photo_bytes = await photo_file.download_as_bytearray()
        np_arr = np.frombuffer(photo_bytes, np.uint8)
        image = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)

        results = reader.readtext(image, detail=0)
        barcodes = [r for r in results if r.isdigit() and len(r) >= 8]

        if barcodes:
            code = barcodes[0]
            await message.reply_text(f"Распознанный код: {code}")
        else:
            await message.reply_text("Не удалось распознать штрихкод. Попробуй ещё или введи вручную.")
            return CODE
    else:
        code = message.text.strip()

    context.user_data['code'] = code
    product_ref = db.collection("products").document(code)
    doc = product_ref.get()
    user_id = update.effective_user.id

    if doc.exists:
        data = doc.to_dict()
        reviews = product_ref.collection("reviews").stream()
        reviews_data = []
        user_review = None

        for r in reviews:
            review = r.to_dict()
            review["_ref"] = r.reference
            reviews_data.append(review)
            if review.get("user_id") == user_id:
                user_review = review

        if user_review:
            context.user_data['existing_review_ref'] = user_review["_ref"]
            context.user_data['existing_review_text'] = user_review["review_text"]
            context.user_data['existing_review_rating'] = user_review["rating"]

            other_reviews = [r for r in reviews_data if r.get("user_id") != user_id]
            review_text = f"Товар: {data.get('name')}\n\n"

            if other_reviews:
                other_reviews.sort(key=lambda r: r.get("rating", 0))
                worst = other_reviews[0]
                best = other_reviews[-1]
                review_text += (
                    f"🟥 Худший отзыв:\n⭐ {worst.get('rating')} — {worst.get('review_text')}\n\n"
                    f"🟩 Лучший отзыв:\n⭐ {best.get('rating')} — {best.get('review_text')}\n\n"
                )
            else:
                review_text += "Пока нет других отзывов.\n\n"

            review_text += (
                f"🟦 Твой отзыв:\n⭐ {user_review['rating']} — {user_review['review_text']}\n\n"
                "Хочешь изменить свой отзыв?"
            )

            await message.reply_text(review_text, reply_markup=yes_no_keyboard)
            return CONFIRM_UPDATE

        else:
            await message.reply_text(
                f"Товар: {data.get('name')}\n\nОтзывы:\n" +
                "\n".join([f"⭐ {r['rating']} — {r['review_text']}" for r in reviews_data]) +
                "\n\nПожалуйста, оцени товар от 1 до 5:",
                reply_markup=quality_keyboard
            )
            return QUALITY
    else:
        await message.reply_text("Товар не найден в базе. Ищу в Open Food Facts...")
        product_info = await fetch_product_info(code)
        if product_info:
            name = product_info.get("name")
            brands = product_info.get("brands")
            full_name = f"{name} ({brands})" if brands else name
            context.user_data['name'] = full_name
            await message.reply_text(
                f"Найден товар:\n{full_name}\n\n"
                "Пожалуйста, оцени качество товара от 1 до 5:",
                reply_markup=quality_keyboard
            )
            return QUALITY
        else:
            await message.reply_text(
                "Товар не найден в интернете.\n"
                "Введи название товара вручную:"
            )
            return QUALITY

# Подтверждение редактирования
async def confirm_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.message.text.lower()
    if answer == "да":
        await update.message.reply_text(
            "Хорошо. Оцени товар от 1 до 5:",
            reply_markup=quality_keyboard
        )
        return QUALITY
    else:
        await update.message.reply_text(
            "Ок, отзыв оставим без изменений. Можешь отправить новый штрихкод или фото."
        )
        context.user_data.clear()
        return CODE

# Оценка качества
async def quality_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    quality = update.message.text.strip()
    if quality not in ['1', '2', '3', '4', '5']:
        await update.message.reply_text(
            "Пожалуйста, выбери оценку от 1 до 5:",
            reply_markup=quality_keyboard
        )
        return QUALITY

    context.user_data['quality'] = int(quality)
    await update.message.reply_text(
        "Оставь отзыв о товаре:",
        reply_markup=ReplyKeyboardRemove()
    )
    return REVIEW

# Отзыв
async def review_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['review'] = update.message.text.strip()
    code = context.user_data['code']
    name = context.user_data.get('name', 'Без названия')

    product_ref = db.collection("products").document(code)
    product_ref.set({"name": name}, merge=True)

    if 'existing_review_ref' in context.user_data:
        context.user_data['existing_review_ref'].set({
            "user_id": update.effective_user.id,
            "rating": context.user_data['quality'],
            "review_text": context.user_data['review'],
            "timestamp": firestore.SERVER_TIMESTAMP
        })
    else:
        product_ref.collection("reviews").add({
            "user_id": update.effective_user.id,
            "rating": context.user_data['quality'],
            "review_text": context.user_data['review'],
            "timestamp": firestore.SERVER_TIMESTAMP
        })

    await update.message.reply_text(
        "Спасибо! Отзыв добавлен в базу.\n\n"
        "Теперь ты можешь отправить новое фото с штрихкодом или ввести код вручную."
    )
    context.user_data.clear()
    return CODE

# Отмена
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Операция отменена.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

# Запуск
def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CODE: [MessageHandler(filters.TEXT | filters.PHOTO, code_handler)],
            QUALITY: [MessageHandler(filters.TEXT, quality_handler)],
            REVIEW: [MessageHandler(filters.TEXT, review_handler)],
            CONFIRM_UPDATE: [MessageHandler(filters.TEXT, confirm_update_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(conv_handler)
    app.run_polling()

if __name__ == "__main__":
    main()
