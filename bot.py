import os
import logging
import asyncio
import math
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import requests
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, WebAppInfo, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    ContextTypes, filters
)

load_dotenv()
logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("reefer-dispatch")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CARGOSYNC_WEBAPP_URL = os.getenv("CARGOSYNC_WEBAPP_URL", "").strip()
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
USER_AGENT = "ReeferDispatchAI/1.1 (personal dispatch assistant)"

TRUCK, PICKUP, DELIVERY, RATE, TARGET = range(5)
_menu_rows = []
if CARGOSYNC_WEBAPP_URL:
    if not CARGOSYNC_WEBAPP_URL.startswith("https://"):
        raise SystemExit("CARGOSYNC_WEBAPP_URL must be a public HTTPS URL")
    schedule_url = CARGOSYNC_WEBAPP_URL
    if "view=" not in schedule_url:
        schedule_url += ("&" if "?" in schedule_url else "?") + "view=schedule"
    _menu_rows.append([KeyboardButton("🗓 CargoSync Schedule", web_app=WebAppInfo(url=schedule_url))])
_menu_rows.extend([
    ["🚛 Calculate load route", "🧮 Calculate RPM"],
    ["📞 Broker counteroffer", "❄️ Reefer checklist"],
    ["🚚 Help"],
])
MENU = ReplyKeyboardMarkup(_menu_rows, resize_keyboard=True)
MENU_BUTTONS = filters.Regex(
    r"^(🚛 Calculate load route|🧮 Calculate RPM|📞 Broker counteroffer|❄️ Reefer checklist|🚚 Help|🗓 CargoSync Schedule)$"
)
USER_TEXT = filters.TEXT & ~filters.COMMAND & ~MENU_BUTTONS

HELP_TEXT = (
    "🚛 *Reefer Dispatch AI — free route beta*\n\n"
    "/load — calculate route, RPM and load analysis\n"
    "/rpm rate loaded_miles deadhead_miles target_rpm\n"
    "Example: `/rpm 2200 850 75 2.50`\n"
    "/counter current_offer target_rate reason — draft a counteroffer\n"
    "/reefer — reefer load checklist\n"
    "/cancel — cancel route entry\n"
    "/help — help\n"
    + ("/app — open CargoSync Schedule\n\n" if CARGOSYNC_WEBAPP_URL else "\n")
    + "Route estimates use public OpenStreetMap services and are not truck-specific. "
    "Confirm truck-legal routing, restrictions and HOS separately before dispatch."
)

REEFER_CHECKLIST = (
    "❄️ *Reefer load checklist*\n\n"
    "1. Commodity and exact setpoint (°F)\n"
    "2. Continuous or Cycle-Sentry mode\n"
    "3. Pre-cool and temperature-recording requirements\n"
    "4. Loaded weight and trailer requirements\n"
    "5. Pickup/delivery appointments and time zones\n"
    "6. All stops and extra-stop pay\n"
    "7. Lumper, detention, layover and TONU terms\n"
    "8. Seal and tracking requirements\n"
    "9. HOS feasibility and realistic transit time\n"
    "10. Rate confirmation matches agreed terms"
)


def parse_decimal(value: str) -> Decimal:
    cleaned = value.strip().replace(",", "").replace("$", "")
    cleaned = cleaned.replace("USD", "").replace("usd", "").strip()
    if not cleaned:
        raise InvalidOperation("empty number")
    number = Decimal(cleaned)
    if not number.is_finite():
        raise InvalidOperation("number must be finite")
    return number


def money(value: Decimal) -> str:
    return f"${value:,.2f}"


def geocode(address: str):
    response = requests.get(
        NOMINATIM_URL,
        params={"q": address, "format": "jsonv2", "limit": 1, "countrycodes": "us"},
        headers={"User-Agent": USER_AGENT},
        timeout=20,
    )
    response.raise_for_status()
    results = response.json()
    if not results:
        raise ValueError(f"Could not find address: {address}")
    item = results[0]
    return float(item["lon"]), float(item["lat"]), item.get("display_name", address)


def route_miles(start, end):
    coords = f"{start[0]},{start[1]};{end[0]},{end[1]}"
    response = requests.get(
        f"{OSRM_URL}/{coords}",
        params={"overview": "false", "steps": "false"},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if data.get("code") != "Ok" or not data.get("routes"):
        raise ValueError("The routing service could not calculate this leg.")
    route = data["routes"][0]
    miles = float(route["distance"]) / 1609.344
    minutes = float(route["duration"]) / 60
    if not math.isfinite(miles) or not math.isfinite(minutes) or miles <= 0:
        raise ValueError("The routing service returned an invalid or zero-length route.")
    return miles, minutes


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("load", None)
    await update.effective_message.reply_text(
        "Hey! I'm your Reefer Dispatch helper. Tap *Calculate load route* or use /load.",
        parse_mode="Markdown", reply_markup=MENU,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(HELP_TEXT, parse_mode="Markdown", reply_markup=MENU)


async def load_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["load"] = {}
    await update.effective_message.reply_text(
        "Let's calculate a load. Enter the truck's current city/state or address (USA).\n"
        "Example: `Chicago, IL`",
        parse_mode="Markdown",
    )
    return TRUCK


async def truck_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.effective_message.text.strip()
    if not value:
        await update.effective_message.reply_text("Enter a truck location, e.g. Chicago, IL.")
        return TRUCK
    context.user_data["load"]["truck"] = value
    await update.effective_message.reply_text("Enter pickup city/state or address. Example: `Columbus, OH`", parse_mode="Markdown")
    return PICKUP


async def pickup_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.effective_message.text.strip()
    if not value:
        await update.effective_message.reply_text("Enter a pickup location, e.g. Columbus, OH.")
        return PICKUP
    context.user_data["load"]["pickup"] = value
    await update.effective_message.reply_text("Enter delivery city/state or address. Example: `Atlanta, GA`", parse_mode="Markdown")
    return DELIVERY


async def delivery_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    value = update.effective_message.text.strip()
    if not value:
        await update.effective_message.reply_text("Enter a delivery location, e.g. Atlanta, GA.")
        return DELIVERY
    context.user_data["load"]["delivery"] = value
    await update.effective_message.reply_text("Enter the total load rate in USD. Example: `3200`", parse_mode="Markdown")
    return RATE


async def rate_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        rate = parse_decimal(update.effective_message.text)
        if rate <= 0:
            raise ValueError("rate must be positive")
        context.user_data["load"]["rate"] = rate
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text("Enter a positive rate, e.g. 3200 or $3,200.")
        return RATE
    await update.effective_message.reply_text("Enter your target all-in RPM. Example: `2.50`", parse_mode="Markdown")
    return TARGET


async def target_step(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target = parse_decimal(update.effective_message.text)
        if target <= 0:
            raise ValueError("target must be positive")
        context.user_data["load"]["target"] = target
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text("Enter a positive target RPM, e.g. 2.50.")
        return TARGET

    data = dict(context.user_data["load"])
    await update.effective_message.reply_text("Calculating addresses and route… please wait.")
    try:
        result = await asyncio.to_thread(calculate_load, data)
        await update.effective_message.reply_text(result, parse_mode="Markdown", disable_web_page_preview=True)
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        log.exception("Route calculation failed")
        await update.effective_message.reply_text(
            "I couldn't calculate this route. Check the address spelling or try again later.\n"
            f"Reason: {str(exc)[:180]}\n\nUse /load to retry."
        )
    except Exception:
        log.exception("Unexpected route calculation failure")
        await update.effective_message.reply_text("An unexpected error occurred. Please use /load to try again.")
    finally:
        context.user_data.pop("load", None)
    return ConversationHandler.END


def analyze_load(rate: Decimal, deadhead: float, loaded: float, target_rpm: Decimal):
    total = deadhead + loaded
    if total <= 0 or loaded <= 0:
        raise ValueError("Route distances must be greater than zero.")
    rate_f = float(rate)
    target_f = float(target_rpm)
    all_in = rate_f / total
    loaded_rpm = rate_f / loaded
    deadhead_pct = deadhead / total * 100
    target_rate = total * target_f
    score = 100
    reasons = []
    if all_in < target_f:
        score -= 35
        reasons.append(f"rate is ${target_rate - rate_f:,.0f} below target")
    else:
        reasons.append("all-in RPM meets target")
    if deadhead_pct > 25:
        score -= 25
        reasons.append(f"deadhead is high ({deadhead_pct:.0f}% of total miles)")
    elif deadhead_pct > 15:
        score -= 12
        reasons.append(f"deadhead is moderate ({deadhead_pct:.0f}% of total miles)")
    else:
        reasons.append(f"deadhead is {deadhead_pct:.0f}% of total miles")
    if loaded_rpm < target_f:
        score -= 10
        reasons.append("loaded RPM is below target")
    score = max(0, min(100, score))
    if all_in >= target_f and deadhead_pct <= 15:
        verdict, advice = "✅ *STRONG CANDIDATE*", "Worth reviewing first; verify appointments and truck-legal routing."
    elif all_in >= target_f and deadhead_pct <= 25:
        verdict, advice = "🟡 *REVIEW CAREFULLY*", "Target RPM is met, but check deadhead and post-delivery repositioning."
    elif all_in >= target_f:
        verdict, advice = "🟡 *HIGH DEADHEAD — REVIEW*", "RPM meets target, but empty miles are significant. Consider a closer truck or negotiation."
    else:
        verdict, advice = "🔴 *COUNTEROFFER OR PASS*", f"To meet ${target_f:.2f} all-in RPM, target at least ${target_rate:,.0f}."
    return verdict, score, deadhead_pct, advice, reasons, target_rate


def calculate_load(data):
    # Nominatim's public endpoint asks users to stay at or below 1 request/second.
    truck = geocode(data["truck"])
    time.sleep(1.05)
    pickup = geocode(data["pickup"])
    time.sleep(1.05)
    delivery = geocode(data["delivery"])
    deadhead, deadhead_min = route_miles(truck, pickup)
    loaded, loaded_min = route_miles(pickup, delivery)
    total = deadhead + loaded
    if total <= 0 or loaded <= 0:
        raise ValueError("The calculated route has zero miles; please check the locations.")

    rate = data["rate"]
    target = data["target"]
    all_in_rpm = float(rate) / total
    loaded_rpm = float(rate) / loaded
    target_rate = Decimal(str(round(total, 4))) * target
    verdict, score, deadhead_pct, advice, reasons, _ = analyze_load(rate, deadhead, loaded, target)

    maps_url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={quote(data['truck'])}"
        f"&waypoints={quote(data['pickup'])}"
        f"&destination={quote(data['delivery'])}"
    )
    return (
        "🚛 *Load route estimate*\n\n"
        f"*Truck:* {data['truck']}\n"
        f"*Pickup:* {data['pickup']}\n"
        f"*Delivery:* {data['delivery']}\n\n"
        f"*Deadhead:* {deadhead:.1f} mi ({deadhead_min/60:.1f} hr est.)\n"
        f"*Loaded miles:* {loaded:.1f} mi ({loaded_min/60:.1f} hr est.)\n"
        f"*Total miles:* {total:.1f} mi\n"
        f"*Estimated driving time:* {(deadhead_min+loaded_min)/60:.1f} hr (routing estimate, not HOS)\n\n"
        f"*Rate:* {money(rate)}\n"
        f"*All-in RPM:* ${all_in_rpm:.2f}/mi\n"
        f"*Loaded RPM:* ${loaded_rpm:.2f}/loaded mi\n"
        f"*Rate needed for target ${target:.2f} all-in RPM:* {money(target_rate)}\n\n"
        f"📊 *Load Analyzer: {score}/100*\n{verdict}\n"
        f"*Deadhead share:* {deadhead_pct:.1f}%\n*Assessment:* {advice}\n"
        f"*Why:* {'; '.join(reasons)}\n\n"
        f"[Open route in Google Maps]({maps_url})\n\n"
        "Route miles and time are estimates, not truck-specific routing. Verify truck restrictions and HOS before dispatch."
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("load", None)
    await update.effective_message.reply_text("Cancelled. Use /load when you're ready.", reply_markup=MENU)
    return ConversationHandler.END


async def app_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not CARGOSYNC_WEBAPP_URL:
        await update.effective_message.reply_text(
            "CargoSync Schedule is not connected yet. Set CARGOSYNC_WEBAPP_URL in the bot's .env after deploying the website."
        )
        return
    schedule_url = CARGOSYNC_WEBAPP_URL if "view=" in CARGOSYNC_WEBAPP_URL else CARGOSYNC_WEBAPP_URL + ("&" if "?" in CARGOSYNC_WEBAPP_URL else "?") + "view=schedule"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Open CargoSync Schedule", web_app=WebAppInfo(url=schedule_url))]])
    await update.effective_message.reply_text(
        "Open the shared CargoSync workspace. Sign in with your team account to view the Schedule and Route Planner.",
        reply_markup=keyboard,
    )


async def rpm_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) != 4:
        await update.effective_message.reply_text("Use: /rpm rate loaded_miles deadhead_miles target_rpm\nExample: /rpm 2200 850 75 2.50")
        return
    try:
        rate, loaded, deadhead, target = map(parse_decimal, context.args)
        if rate <= 0 or loaded <= 0 or deadhead < 0 or target <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text("Enter valid positive rate, loaded miles and target RPM; deadhead cannot be negative.")
        return
    total = loaded + deadhead
    await update.effective_message.reply_text(
        f"🧮 *Load calculation*\n\nRate: {money(rate)}\nLoaded: {loaded} mi\nDeadhead: {deadhead} mi\n"
        f"Total: {total} mi\nAll-in RPM: *${rate/total:.2f} / mi*\nLoaded RPM: ${rate/loaded:.2f} / loaded mi\n"
        f"Target rate: {money(total*target)}",
        parse_mode="Markdown", reply_markup=MENU,
    )


async def counter_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.effective_message.reply_text("Use: /counter current_offer target_rate reason\nExample: /counter 2200 2450 deadhead is high")
        return
    try:
        offer = parse_decimal(context.args[0])
        target = parse_decimal(context.args[1])
        if offer <= 0 or target <= 0:
            raise ValueError
    except (InvalidOperation, ValueError):
        await update.effective_message.reply_text("Enter positive offer and target amounts.")
        return
    reason = " ".join(context.args[2:]) or "miles and operating costs"
    # Markdown special characters in free-form reason are avoided by plain text output.
    await update.effective_message.reply_text(
        f"📞 Broker counteroffer draft\n\nThanks for the offer. Considering the total mileage and operating costs, "
        f"we would need ${target:,.0f} to make this load work. Is there flexibility to improve the rate from "
        f"${offer:,.0f} to ${target:,.0f}? The main concern is {reason}. We can move forward promptly if we can get closer to that number.",
        reply_markup=MENU,
    )


async def reefer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(REEFER_CHECKLIST, parse_mode="Markdown", reply_markup=MENU)


async def button_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.effective_message.text
    if text == "🚛 Calculate load route":
        return await load_start(update, context)
    if text in {"🗓 Open CargoSync", "🗓 CargoSync Schedule"}:
        await app_cmd(update, context)
    elif text == "🧮 Calculate RPM":
        await update.effective_message.reply_text("Use /rpm rate loaded_miles deadhead_miles target_rpm", reply_markup=MENU)
    elif text == "📞 Broker counteroffer":
        await update.effective_message.reply_text("Use /counter current_offer target_rate reason", reply_markup=MENU)
    elif text == "❄️ Reefer checklist":
        await reefer_cmd(update, context)
    elif text == "🚚 Help":
        await help_cmd(update, context)
    else:
        await update.effective_message.reply_text("Use /load to calculate a route or /help for commands.", reply_markup=MENU)
    return ConversationHandler.END


async def menu_during_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Menu taps intentionally end the active data-entry flow, except starting a new route.
    context.user_data.pop("load", None)
    return await button_text(update, context)


async def command_during_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("load", None)
    command = update.effective_message.text.split()[0].split("@")[0].lower()
    if command == "/start":
        await start(update, context)
    elif command == "/help":
        await help_cmd(update, context)
    elif command == "/rpm":
        await rpm_cmd(update, context)
    elif command == "/counter":
        await counter_cmd(update, context)
    elif command == "/reefer":
        await reefer_cmd(update, context)
    elif command == "/app":
        await app_cmd(update, context)
    else:
        await update.effective_message.reply_text("Use /load to start a route or /cancel to stop.", reply_markup=MENU)
    return ConversationHandler.END


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing. Add it to .env next to bot.py.")
    app = Application.builder().token(TOKEN).concurrent_updates(False).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("load", load_start), MessageHandler(filters.Regex(r"^🚛 Calculate load route$"), load_start)],
        states={
            TRUCK: [MessageHandler(USER_TEXT, truck_step)],
            PICKUP: [MessageHandler(USER_TEXT, pickup_step)],
            DELIVERY: [MessageHandler(USER_TEXT, delivery_step)],
            RATE: [MessageHandler(USER_TEXT, rate_step)],
            TARGET: [MessageHandler(USER_TEXT, target_step)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CommandHandler(["start", "help", "rpm", "counter", "reefer"], command_during_conversation),
            MessageHandler(MENU_BUTTONS, menu_during_conversation),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("rpm", rpm_cmd))
    app.add_handler(CommandHandler("counter", counter_cmd))
    app.add_handler(CommandHandler("reefer", reefer_cmd))
    app.add_handler(CommandHandler("app", app_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, button_text))
    log.info("Reefer Dispatch AI is running.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()