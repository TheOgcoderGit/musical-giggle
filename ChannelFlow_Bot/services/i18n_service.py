"""
ChannelFlow AI - Internationalization (i18n)
==============================================

Translation-key system (Prompt 1 §19): user-facing strings are looked
up by key in the user's language, falling back to English, falling
back to the key itself.

Languages: en, hi, bn, ur, es, ar, id (extensible - add a dict).

Usage:
    from services import i18n
    i18n.t(user_id, "project.created", name="Deals")

DB `users.language` is the source of truth; translations dict below is
seeded at import and can be extended via the translations table later.
"""

from database.db import get_connection

LANGUAGES = {
    "en": "English",
    "hi": "हिंदी",
    "bn": "বাংলা",
    "ur": "اردو",
    "es": "Español",
    "ar": "العربية",
    "id": "Bahasa Indonesia",
}

# Core-flow strings only - full coverage lands per-screen as features
# stabilize. Missing keys fall back to English then to the key.
_STRINGS = {
    "welcome.title": {
        "en": "👋 Welcome, creator, to ChannelFlow AI!",
        "hi": "👋 स्वागत है, क्रिएटर, ChannelFlow AI में!",
        "bn": "👋 স্বাগতম, ক্রিয়েটর, ChannelFlow AI-তে!",
        "ur": "👋 خوش آمدید، کریئٹر، ChannelFlow AI میں!",
        "es": "👋 ¡Bienvenido, creador, a ChannelFlow AI!",
        "ar": "👋 مرحباً بك أيها المبدع في ChannelFlow AI!",
        "id": "👋 Selamat datang, kreator, di ChannelFlow AI!",
    },
    "home.welcome_back": {
        "en": "Welcome back",
        "hi": "वापसी पर स्वागत है",
        "bn": "আবার স্বাগতম",
        "ur": "خوش آمدید",
        "es": "Bienvenido de nuevo",
        "ar": "مرحباً بعودتك",
        "id": "Selamat kembali",
    },
    "plan.label": {
        "en": "Plan", "hi": "प्लान", "bn": "প্ল্যান", "ur": "پلان",
        "es": "Plan", "ar": "الخطة", "id": "Paket",
    },
    "projects.label": {
        "en": "Projects", "hi": "प्रोजेक्ट्स", "bn": "প্রজেক্টস", "ur": "پروجیکٹس",
        "es": "Proyectos", "ar": "المشاريع", "id": "Proyek",
    },
    "wallet.insufficient_balance": {
        "en": "Insufficient wallet balance.",
        "hi": "वॉलेट बैलेंस अपर्याप्त है।",
        "bn": "ওয়ালেট ব্যালেন্স অপর্যাপ্ত।",
        "ur": "والیٹ بیلنس ناکافی ہے۔",
        "es": "Saldo de billetera insuficiente.",
        "ar": "رصيد المحفظة غير كافٍ.",
        "id": "Saldo dompet tidak mencukupi.",
    },
    "subscription.expiring": {
        "en": "Your {plan} plan expires in {days} days.",
        "hi": "आपका {plan} प्लान {days} दिनों में समाप्त होगा।",
        "bn": "আপনার {plan} প্ল্যান {days} দিনের মধ্যে শেষ হবে।",
        "ur": "آپ کا {plan} پلان {days} دن میں ختم ہو جائے گا۔",
        "es": "Tu plan {plan} vence en {days} días.",
        "ar": "تنتهي صلاحية خطتك {plan} خلال {days} يومًا.",
        "id": "Paket {plan} Anda berakhir dalam {days} hari.",
    },
    "project.created": {
        "en": "Project created: {name}",
        "hi": "प्रोजेक्ट बनाया गया: {name}",
        "bn": "প্রজেক্ট তৈরি হয়েছে: {name}",
        "ur": "پروجیکٹ بنایا گیا: {name}",
        "es": "Proyecto creado: {name}",
        "ar": "تم إنشاء المشروع: {name}",
        "id": "Proyek dibuat: {name}",
    },
    "giveaway.winner": {
        "en": "🎉 You won the giveaway!",
        "hi": "🎉 आपने गिवअवे जीता!",
        "bn": "🎉 আপনি গিভঅ্যাওয়ে জিতেছেন!",
        "ur": "🎉 آپ نے گیو اوے جیت لیا!",
        "es": "¡🎉 Ganaste el sorteo!",
        "ar": "🎉 لقد فزت بالسحب!",
        "id": "🎉 Anda memenangkan giveaway!",
    },
    "support.ticket_created": {
        "en": "Support ticket #{id} created. We'll reply soon.",
        "hi": "सपोर्ट टिकट #{id} बनाया गया। हम जल्द जवाब देंगे।",
        "bn": "সাপোর্ট টিকিট #{id} তৈরি হয়েছে। আমরা শীঘ্রই উত্তর দেব।",
        "ur": "سپورٹ ٹکٹ #{id} بنا دیا گیا۔ ہم جلد جواب دیں گے۔",
        "es": "Ticket de soporte #{id} creado. Responderemos pronto.",
        "ar": "تم إنشاء تذكرة الدعم #{id}. سنرد قريباً.",
        "id": "Tiket dukungan #{id} dibuat. Kami akan segera menjawab.",
    },
    "payment.approved": {
        "en": "✅ Your payment was approved - you're now on {plan}!",
        "hi": "✅ आपका भुगतान स्वीकृत हो गया - अब आप {plan} पर हैं!",
        "bn": "✅ আপনার পেমেন্ট অনুমোদিত হয়েছে - আপনি এখন {plan}-এ!",
        "ur": "✅ آپ کی ادائیگی منظور ہو گئی - اب آپ {plan} پر ہیں!",
        "es": "✅ Pago aprobado - ¡ya estás en {plan}!",
        "ar": "✅ تمت الموافقة على دفعتك - أنت الآن على {plan}!",
        "id": "✅ Pembayaran disetujui - Anda sekarang di {plan}!",
    },
    "language.updated": {
        "en": "Language updated successfully.",
        "hi": "भाषा सफलतापूर्वक अपडेट की गई।",
        "bn": "ভাষা সফলভাবে আপডেট হয়েছে।",
        "ur": "زبان کامیابی سے اپ ڈیٹ ہو گئی۔",
        "es": "Idioma actualizado correctamente.",
        "ar": "تم تحديث اللغة بنجاح.",
        "id": "Bahasa berhasil diperbarui.",
    },
    "whatsapp.destination_added": {
        "en": "✅ WhatsApp destination added!\n\n🔑 Pairing code: `{code}`\n\nThis code expires in {minutes} minutes.\n\nSend this code to the WhatsApp bot to bind this project.",
        "hi": "✅ व्हाट्सएप डेस्टिनेशन जोड़ा गया!\n\n🔑 पेयरिंग कोड: `{code}`\n\nयह कोड {minutes} मिनट में समाप्त होगा।\n\nप्रोजेक्ट बाइंड करने के लिए यह कोड व्हाट्सएप बॉट को भेजें।",
        "bn": "✅ WhatsApp ডেস্টিনেশন যোগ হয়েছে!\n\n🔑 পেয়ারিং কোড: `{code}`\n\nএই কোডটি {minutes} মিনিটে শেষ হবে।\n\nপ্রজেক্ট বাইন্ড করতে এই কোডটি WhatsApp বটে পাঠান।",
        "ur": "✅ واٹس ایپ ڈیسٹینیشن شامل ہو گئی!\n\n🔑 پیئرنگ کوڈ: `{code}`\n\nیہ کوڈ {minutes} منٹ میں ختم ہو جائے گا۔\n\nپروجیکٹ بائنڈ کرنے کے لیے یہ کوڈ واٹس ایپ بوٹ کو بھیجیں۔",
        "es": "✅ Destino de WhatsApp añadido!\n\n🔑 Código de emparejamiento: `{code}`\n\nEste código caduca en {minutes} minutos.\n\nEnvía este código al bot de WhatsApp para vincular este proyecto.",
        "ar": "✅ تمت إضافة وجهة واتساب!\n\n🔑 رمز الاقتران: `{code}`\n\nينتهي هذا الرمز خلال {minutes} دقيقة.\n\nأرسل هذا الرمز إلى بوت واتساب لربط هذا المشروع.",
        "id": "✅ Destinasi WhatsApp ditambahkan!\n\n🔑 Kode pairing: `{code}`\n\nKode ini kedaluwarsa dalam {minutes} menit.\n\nKirim kode ini ke bot WhatsApp untuk mengikat proyek ini.",
    },
    "whatsapp.pairing_verified": {
        "en": "✅ WhatsApp destination verified successfully!",
        "hi": "✅ व्हाट्सएप डेस्टिनेशन सफलतापूर्वक सत्यापित!",
        "bn": "✅ WhatsApp ডেস্টিনেশন সফলভাবে যাচাই হয়েছে!",
        "ur": "✅ واٹس ایپ ڈیسٹینیشن کامیابی سے تصدیق شدہ!",
        "es": "✅ ¡Destino de WhatsApp verificado correctamente!",
        "ar": "✅ تم التحقق من وجهة واتساب بنجاح!",
        "id": "✅ Destinasi WhatsApp berhasil diverifikasi!",
    },
    "whatsapp.destination_test_pass": {
        "en": "✅ Test Passed\n\n📤 {label} - connection verified.",
        "hi": "✅ टेस्ट सफल\n\n📤 {label} - कनेक्शन सत्यापित।",
        "bn": "✅ টেস্ট সফল\n\n📤 {label} - সংযোগ যাচাই হয়েছে।",
        "ur": "✅ ٹیسٹ کامیاب\n\n📤 {label} - کنکشن تصدیق شدہ۔",
        "es": "✅ Prueba superada\n\n📤 {label} - conexión verificada.",
        "ar": "✅ تم اجتياز الاختبار\n\n📤 {label} - تم التحقق من الاتصال.",
        "id": "✅ Tes berhasil\n\n📤 {label} - koneksi terverifikasi.",
    },
    "whatsapp.destination_test_fail": {
        "en": "❌ Test Failed\n\n📤 {label}\n\n{detail}",
        "hi": "❌ टेस्ट विफल\n\n📤 {label}\n\n{detail}",
        "bn": "❌ টেস্ট ব্যর্থ\n\n📤 {label}\n\n{detail}",
        "ur": "❌ ٹیسٹ ناکام\n\n📤 {label}\n\n{detail}",
        "es": "❌ Prueba fallida\n\n📤 {label}\n\n{detail}",
        "ar": "❌ فشل الاختبار\n\n📤 {label}\n\n{detail}",
        "id": "❌ Tes gagal\n\n📤 {label}\n\n{detail}",
    },
    "whatsapp.pairing_expired": {
        "en": "Pairing code has expired. Please generate a new one.",
        "hi": "पेयरिंग कोड समाप्त हो गया है। कृपया नया कोड बनाएं।",
        "bn": "পেয়ারিং কোডের মেয়াদ শেষ হয়েছে। অনুগ্রহ করে নতুন কোড তৈরি করুন।",
        "ur": "پیئرنگ کوڈ کی میعاد ختم ہو گئی۔ براہ کرم نیا کوڈ بنائیں۔",
        "es": "El código de emparejamiento ha caducado. Genera uno nuevo.",
        "ar": "انتهت صلاحية رمز الاقتران. يرجى إنشاء رمز جديد.",
        "id": "Kode pairing telah kedaluwarsa. Silakan buat yang baru.",
    },
    "source.health_alert": {
        "en": "⚠️ Source Health Alert: {count} unhealthy source(s) detected.",
        "hi": "⚠️ सोर्स हेल्थ अलर्ट: {count} अस्वस्थ सोर्स पाए गए।",
        "bn": "⚠️ সোর্স হেলথ অ্যালার্ট: {count}টি অস্বাস্থ্যকর সোর্স পাওয়া গেছে।",
        "ur": "⚠️ سورس ہیلتھ الرٹ: {count} غیر صحت مند سورسز ملے۔",
        "es": "⚠️ Alerta de salud de fuente: {count} fuentes no saludables detectadas.",
        "ar": "⚠️ تنبيه صحة المصدر: تم اكتشاف {count} مصدر غير صحي.",
        "id": "⚠️ Peringatan kesehatan sumber: {count} sumber tidak sehat terdeteksi.",
    },
    "error.generic_forward": {
        "en": "Something went wrong while forwarding. Please try again.",
        "hi": "फ़ॉर्वर्ड करते समय कुछ गलत हुआ। कृपया पुनः प्रयास करें।",
        "bn": "ফরওয়ার্ড করার সময় কিছু ভুল হয়েছে। অনুগ্রহ করে আবার চেষ্টা করুন।",
        "ur": "فارورڈ کرتے وقت کچھ غلط ہو گیا۔ براہ کرم دوبارہ کوشش کریں۔",
        "es": "Algo salió mal al reenviar. Inténtalo de nuevo.",
        "ar": "حدث خطأ أثناء إعادة التوجيه. يرجى المحاولة مرة أخرى.",
        "id": "Terjadi kesalahan saat meneruskan. Silakan coba lagi.",
    },

    "lang.prompt": {
        "en": "🌐 Choose your preferred language:",
        "hi": "🌐 अपनी पसंदीदा भाषा चुनें:",
    },
    "lang.saved": {
        "en": "✅ Language set. Restarting your menu in your language...",
        "hi": "✅ भाषा सेट हो गई। मेनू आपकी भाषा में दिखेगा...",
    },
    "nav.home_connected": {
        "en": "🏠 ChannelFlow AI\n\nWelcome back, {name}!\n\n🔗 Account: Connected\n\nChoose what you'd like to do:",
        "hi": "🏠 ChannelFlow AI\n\nवापसी पर स्वागत है, {name}!\n\n🔗 अकाउंट: कनेक्टेड\n\nआप क्या करना चाहेंगे:",
    },
    "nav.home_unconnected": {
        "en": "🏠 ChannelFlow AI\n\nHi {name}!\n\n🔗 Account: Not connected\n\nConnect your Telegram account to start automating forwards.",
        "hi": "🏠 ChannelFlow AI\n\nनमस्ते {name}!\n\n🔗 अकाउंट: कनेक्ट नहीं है\n\nफ़ॉर्वर्डिंग शुरू करने के लिए अपना Telegram अकाउंट कनेक्ट करें।",
    },
    "nav.why_connect": {
        "en": "🔐 Why connect your Telegram account?\n\nChannelFlow forwards using YOUR account, so:\n\n• You choose which channels/groups are watched\n• Content is forwarded the way you would send it\n• Your session is encrypted and never shared\n• Disconnect any time to stop everything\n\nYour login code is private - ChannelFlow support will never ask for it.",
        "hi": "🔐 अपना Telegram अकाउंट क्यों कनेक्ट करें?\n\nChannelFlow आपके अकाउंट से फ़ॉर्वर्ड करता है, इसलिए:\n\n• आप तय करते हैं कौन से चैनल/ग्रुप देखे जाएँ\n• कंटेंट आपके भेजने के तरीके से जाता है\n• आपका सेशन एन्क्रिप्टेड है, किसी से शेयर नहीं होता\n• कभी भी डिस्कनेक्ट करके सब रोक सकते हैं\n\nआपका लॉगिन कोड निजी है - ChannelFlow सपोर्ट उसे कभी नहीं माँगेगा।",
    },
    "nav.your_tasks": {
        "en": "📁 Your Tasks\n\nTap a task to open it, or create a new one:",
        "hi": "📁 आपके टास्क\n\nकोई टास्क खोलने के लिए उसे दबाएँ, या नया बनाएँ:",
    },
    "nav.tasks_empty": {
        "en": "📁 Your Tasks\n\nYou haven't created any tasks yet.\n\nCreate your first forwarding task to get started.",
        "hi": "📁 आपके टास्क\n\nआपने अभी कोई टास्क नहीं बनाया है।\n\nशुरू करने के लिए अपना पहला फ़ॉर्वर्डिंग टास्क बनाएँ।",
    },
    "task.details": {
        "en": "📌 {name}\n\nStatus: {status}\nRoute: {route}\n\nSources: {sources}\nDestinations: {destinations}",
        "hi": "📌 {name}\n\nस्थिति: {status}\nरूट: {route}\n\nसोर्स: {sources}\nडेस्टिनेशन: {destinations}",
    },
    "task.deleted": {
        "en": "🗑 Task deleted: {name}",
        "hi": "🗑 टास्क डिलीट हुआ: {name}",
    },
    "task.delete_confirm": {
        "en": "⚠️ Delete Task?\n\nTask: {name}\n\nThis action cannot be undone.",
        "hi": "⚠️ टास्क डिलीट करें?\n\nटास्क: {name}\n\nयह क्रिया वापस नहीं हो सकती।",
    },
    "task.created": {
        "en": "✅ Task created: {name}\n\nNow add a source and a destination to start it.",
        "hi": "✅ टास्क बन गया: {name}\n\nइसे शुरू करने के लिए अब सोर्स और डेस्टिनेशन जोड़ें।",
    },
    "task.started": {
        "en": "🟢 Task started: {name}",
        "hi": "🟢 टास्क शुरू हुआ: {name}",
    },
    "task.stopped": {
        "en": "🔴 Task stopped: {name}",
        "hi": "🔴 टास्क रुक गया: {name}",
    },
    "nav.screen_expired": {
        "en": "This screen has expired. Please open the section again.",
        "hi": "यह स्क्रीन समाप्त हो गई है। कृपया सेक्शन फिर से खोलें।",
    },
    "nav.back": {"en": "◀️ Back", "hi": "◀️ पीछे"},
    "nav.home": {"en": "🏠 Home", "hi": "🏠 होम"},
    "support.why_connect_hint": {
        "en": "🆘 Support\n\nChoose an option below:",
        "hi": "🆘 सपोर्ट\n\nनीचे कोई विकल्प चुनें:",
    },
}

_user_lang_cache = {}


def get_user_language(user_id) -> str:
    if user_id in _user_lang_cache:
        return _user_lang_cache[user_id]

    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute("SELECT language FROM users WHERE telegram_id=?", (user_id,))
        row = cur.fetchone()
        conn.close()
        lang = row["language"] if row and row["language"] else "en"
    except Exception:
        lang = "en"

    _user_lang_cache[user_id] = lang
    return lang


def set_user_language(user_id, lang):
    if lang not in LANGUAGES:
        return False

    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET language=?, language_chosen=1 WHERE telegram_id=?",
        (lang, user_id),
    )
    conn.commit()
    conn.close()

    _user_lang_cache[user_id] = lang
    return True


def t(user_id, key, **kwargs) -> str:
    """Translate `key` for this user's language with {kwargs} formatting.
    Falls back: user lang -> English -> raw key."""

    lang = get_user_language(user_id)

    entry = _STRINGS.get(key) or {}
    text = entry.get(lang) or entry.get("en") or key

    if kwargs:
        try:
            text = text.format(**kwargs)
        except (KeyError, IndexError):
            pass

    return text


def invalidate_cache(user_id=None):
    if user_id is None:
        _user_lang_cache.clear()
    else:
        _user_lang_cache.pop(user_id, None)