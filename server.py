"""
HostingControl — Merkezi Sunucu (Railway)
Depolama: Kullanıcılar Railway env var'da (HC_USERS_DB) tutulur.
          Railway restart etse bile veri kaybolmaz.
          Cihaz/tarama verileri /tmp'de tutulur (geçici, restart'ta sıfırlanır — önemli değil).
"""
import os, json, hashlib, threading, uuid, re, base64, gzip, urllib.request, ssl
from datetime import datetime, timezone, timedelta
from pathlib import Path
from flask import Flask, jsonify, request
from flask_cors import CORS

app  = Flask(__name__)
CORS(app)

HC_SECRET   = os.environ.get("HC_SECRET",   "hc_gizli_anahtar_degistir")
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", HC_SECRET)

# ── RAILWAY ENV VAR DEPOLAMA ──────────────────────────────────
# Kullanıcı DB'si Railway env var'da tutulur → restart'tan etkilenmez.
# Gerekli Railway env var'ları (Railway otomatik inject eder):
#   RAILWAY_PROJECT_ID, RAILWAY_ENVIRONMENT_ID, RAILWAY_SERVICE_ID
# Kullanıcının elle eklemesi gereken tek şey:
#   RAILWAY_TOKEN = Railway dashboard → Account Settings → Tokens

RAILWAY_TOKEN          = os.environ.get("RAILWAY_TOKEN", "")
RAILWAY_PROJECT_ID     = os.environ.get("RAILWAY_PROJECT_ID", "")
RAILWAY_ENVIRONMENT_ID = os.environ.get("RAILWAY_ENVIRONMENT_ID", "")
RAILWAY_SERVICE_ID     = os.environ.get("RAILWAY_SERVICE_ID", "")

# Geçici dosya cache (restart'a kadar geçerli)
TMP = Path("/tmp/hc")
TMP.mkdir(parents=True, exist_ok=True)
DEVICES_FILE = TMP / "devices_db.json"
TARAMA_FILE  = TMP / "taramalar.json"
TARAMA_FILES = TMP / "tarama_dosyalar"
TARAMA_FILES.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()
_clk  = threading.Lock()

# ── KULLANICI DB (BELLEK + ENV VAR) ──────────────────────────
_users_mem: dict = {}   # Ana bellek içi kullanıcı veritabanı
_users_lock = threading.Lock()

def _encode_db(data: dict) -> str:
    """dict → base64(gzip(json))"""
    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return base64.b64encode(gzip.compress(raw)).decode("ascii")

def _decode_db(s: str) -> dict:
    """base64(gzip(json)) → dict"""
    try:
        return json.loads(gzip.decompress(base64.b64decode(s)).decode("utf-8"))
    except Exception:
        return {}

def _railway_env_guncelle(deger: str) -> bool:
    """
    HC_USERS_DB env var'ını Railway GraphQL API ile günceller.
    RAILWAY_TOKEN set edilmemişse sessizce geçer.
    """
    if not RAILWAY_TOKEN or not RAILWAY_PROJECT_ID or not RAILWAY_SERVICE_ID:
        return False
    try:
        env_id = RAILWAY_ENVIRONMENT_ID or "production"
        mutation = """
        mutation VarUpsert($input: VariableUpsertInput!) {
            variableUpsert(input: $input)
        }
        """
        payload = json.dumps({
            "query": mutation,
            "variables": {
                "input": {
                    "projectId":     RAILWAY_PROJECT_ID,
                    "environmentId": env_id,
                    "serviceId":     RAILWAY_SERVICE_ID,
                    "name":          "HC_USERS_DB",
                    "value":         deger
                }
            }
        }).encode("utf-8")
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode    = ssl.CERT_NONE
        req = urllib.request.Request(
            "https://backboard.railway.app/graphql/v2",
            data    = payload,
            headers = {
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {RAILWAY_TOKEN}"
            },
            method  = "POST"
        )
        urllib.request.urlopen(req, timeout=10, context=ctx)
        return True
    except Exception:
        return False

def _env_guncelle_async(encoded: str):
    """Railway API çağrısını arka planda yapar — request'i bloke etmez."""
    t = threading.Thread(target=_railway_env_guncelle, args=(encoded,), daemon=True)
    t.start()

def db_oku() -> dict:
    """Bellek içi kullanıcı DB'sini döndürür."""
    with _users_lock:
        return dict(_users_mem)

def db_yaz(data: dict):
    """
    Kullanıcı DB'sini günceller:
    1. Bellek içi dict güncellenir (anlık)
    2. /tmp cache dosyasına yazılır (restart'a kadar)
    3. Railway env var güncellenir arka planda (kalıcı)
    """
    with _users_lock:
        _users_mem.clear()
        _users_mem.update(data)
    # /tmp cache
    try:
        (TMP / "users_db.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass
    # Railway env var — arka planda
    try:
        encoded = _encode_db(data)
        _env_guncelle_async(encoded)
    except Exception:
        pass

def _db_yukle():
    """
    Sunucu başlarken kullanıcı DB'sini yükler.
    Öncelik sırası:
      1. HC_USERS_DB env var (Railway'de kalıcı)
      2. /tmp/users_db.json (önceki oturumdan cache, aynı container ise)
      3. Boş dict (ilk çalışma)
    """
    global _users_mem

    # 1. Env var
    encoded = os.environ.get("HC_USERS_DB", "")
    if encoded:
        data = _decode_db(encoded)
        if data:
            print(f"[HC] Kullanıcı DB env var'dan yüklendi: {len(data)} kullanıcı")
            with _users_lock:
                _users_mem = data
            return

    # 2. /tmp cache
    cache = TMP / "users_db.json"
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data:
                print(f"[HC] Kullanıcı DB /tmp cache'den yüklendi: {len(data)} kullanıcı")
                with _users_lock:
                    _users_mem = data
                return
        except Exception:
            pass

    print("[HC] Kullanıcı DB boş başlatıldı.")

# Sunucu başlangıcında yükle
_db_yukle()

# ── GEÇİCİ DOSYA (cihaz/tarama) ──────────────────────────────
def _oku(path):
    try:
        if path.exists(): return json.loads(path.read_text(encoding="utf-8"))
    except: pass
    return {}

def _yaz(path, data):
    try: path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except: pass

def dev_oku():
    with _lock: return _oku(DEVICES_FILE)
def dev_yaz(d):
    with _lock: _yaz(DEVICES_FILE, d)
def tarama_oku():
    with _lock: return _oku(TARAMA_FILE)
def tarama_yaz(d):
    with _lock: _yaz(TARAMA_FILE, d)

# ── YARDIMCILAR ──────────────────────────────────────────────
def shash(s): return hashlib.sha256(s.encode()).hexdigest()

def auth_bot():
    s = request.headers.get("X-HC-Secret") or request.args.get("secret","")
    return s == HC_SECRET

def auth_admin():
    s = request.headers.get("X-HC-Secret") or request.args.get("secret","")
    return s in (ADMIN_TOKEN, HC_SECRET)

def parse_iso(s):
    if not s: return None
    try: return datetime.fromisoformat(s.replace("Z","+00:00"))
    except:
        try: return datetime.strptime(s[:19],"%Y-%m-%dT%H:%M:%S")
        except: return None

def expired(u):
    exp = u.get("expires")
    if not exp: return False
    dt = parse_iso(exp)
    if not dt: return False
    now = datetime.now(timezone.utc) if dt.tzinfo else datetime.now()
    return now > dt

def time_left(exp):
    if not exp: return "Sınırsız"
    dt = parse_iso(exp)
    if not dt: return "Sınırsız"
    now = datetime.now(timezone.utc) if dt.tzinfo else datetime.now()
    d = dt - now
    if d.total_seconds() <= 0: return "Süresi Doldu"
    g=d.days; s=d.seconds; h=s//3600; m=(s%3600)//60
    parts=[]
    if g: parts.append(f"{g}g")
    if h: parts.append(f"{h}s")
    if m: parts.append(f"{m}d")
    return " ".join(parts) or "<1d"

_cihazlar = {}

def cihaz_al(mac):
    with _clk:
        if mac not in _cihazlar:
            _cihazlar[mac] = {"komutlar":[],"sonuclar":[],"son_gorulme":None,"hostname":"?","kullanici":"?"}
        return _cihazlar[mac]

def _cihaz_kaydet_db(mac, kadi, info):
    dev=dev_oku(); simdi=datetime.now().isoformat()
    if mac in dev:
        dev[mac]["son_calisma"]=simdi
        dev[mac]["calisma_sayisi"]=dev[mac].get("calisma_sayisi",1)+1
        if kadi and kadi not in dev[mac].get("kullanicilar",[]):
            dev[mac].setdefault("kullanicilar",[]).append(kadi)
    else:
        dev[mac]={"mac":mac,"hostname":info.get("hostname","?"),"username":info.get("username","?"),
                  "win_release":info.get("win_release","?"),"ilk_calisma":simdi,"son_calisma":simdi,
                  "calisma_sayisi":1,"kullanicilar":[kadi] if kadi else [],"engellendi":False}
    dev_yaz(dev)

# ── GENEL ────────────────────────────────────────────────────
@app.route("/ping")
def ping():
    secret_ok = HC_SECRET != "hc_gizli_anahtar_degistir"
    railway_ok = bool(RAILWAY_TOKEN and RAILWAY_PROJECT_ID)
    return jsonify({
        "ok"         : True,
        "zaman"      : datetime.now().isoformat(),
        "secret_set" : secret_ok,
        "railway_ok" : railway_ok,
        "kullanici_say": len(db_oku()),
        "secret_bas" : HC_SECRET[:4] + "***" if secret_ok else "AYARLANMAMIS"
    })

@app.route("/debug")
def debug():
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    db=db_oku(); dev=dev_oku()
    with _clk:
        botlar={mac:{"hostname":v["hostname"],"kullanici":v["kullanici"],"son_gorulme":v["son_gorulme"]} for mac,v in _cihazlar.items()}
    return jsonify({
        "ok":True,"kullanici_sayisi":len(db),"cihaz_sayisi":len(dev),
        "online_bot":len(botlar),"botlar":botlar,
        "railway_token_set": bool(RAILWAY_TOKEN),
        "railway_project_id": RAILWAY_PROJECT_ID[:8]+"..." if RAILWAY_PROJECT_ID else "YOK",
    })

# ── AUTH ──────────────────────────────────────────────────────
@app.route("/auth/kayit", methods=["POST"])
def kayit():
    if not auth_bot(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    d=request.json or {}
    kadi=d.get("kadi","").strip().lower(); sifre=d.get("sifre","").strip()
    site=d.get("site","").strip(); mac=d.get("mac","").strip()
    if not re.match(r"^[a-zA-Z0-9_]{4,20}$",kadi):
        return jsonify({"ok":False,"hata":"Kullanıcı adı 4-20 karakter, harf/rakam/alt çizgi olmalı."})
    if len(sifre)<6: return jsonify({"ok":False,"hata":"Şifre en az 6 karakter olmalı."})
    db=db_oku()
    if kadi in db: return jsonify({"ok":False,"hata":"Bu kullanıcı adı alınmış."})
    for k,v in list(db.items()):
        if mac and mac in v.get("macler",[]):
            if v.get("approved") and not v.get("locked"):
                return jsonify({"ok":False,"hata":"Bu cihazdan zaten kayıt yapılmış."})
            else:
                del db[k]
                db_yaz(db)
                break
    db[kadi]={"pw":shash(sifre),"pw_plain":sifre,"approved":False,"max_dev":1,"expires":None,
               "active":[],"macler":[mac] if mac else [],"locked":False,"site":site,"reg_date":datetime.now().isoformat()}
    db_yaz(db)
    if mac: _cihaz_kaydet_db(mac,kadi,d)
    return jsonify({"ok":True})

@app.route("/auth/giris", methods=["POST"])
def giris():
    if not auth_bot(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    d=request.json or {}
    kadi=d.get("kadi","").strip().lower(); sifre=d.get("sifre","").strip(); mac=d.get("mac","").strip()
    db=db_oku()
    if kadi not in db: return jsonify({"ok":False,"hata":"Kullanıcı bulunamadı."})
    u=db[kadi]
    if u["pw"]!=shash(sifre): return jsonify({"ok":False,"hata":"Şifre hatalı."})
    if not u.get("approved"): return jsonify({"ok":False,"hata":"Hesabınız henüz onaylanmadı. Admin onayını bekleyin."})
    if u.get("locked"): return jsonify({"ok":False,"hata":"Hesabınız kilitlendi. Adminle iletişime geçin."})
    if expired(u): return jsonify({"ok":False,"hata":"Kullanım süreniz doldu. Adminle iletişime geçin."})
    macler=u.get("macler",[]); max_dev=u.get("max_dev",1)
    if mac and mac not in macler:
        if len(macler)>=max_dev: return jsonify({"ok":False,"hata":f"Maksimum cihaz sayısına ({max_dev}) ulaştınız."})
        db[kadi]["macler"].append(mac)
    if mac and mac not in db[kadi].get("active",[]):
        db[kadi].setdefault("active",[]).append(mac)
    db_yaz(db)
    if mac: _cihaz_kaydet_db(mac,kadi,d)
    return jsonify({"ok":True,"kadi":kadi,"site":u.get("site",""),"sure":time_left(u.get("expires")),
                    "max_dev":max_dev,"cihaz_say":len(db[kadi].get("macler",[])),"expires":u.get("expires","")})

@app.route("/auth/cikis", methods=["POST"])
def cikis():
    if not auth_bot(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    d=request.json or {}; kadi=d.get("kadi","").lower(); mac=d.get("mac","")
    db=db_oku()
    if kadi in db and mac:
        aktif=db[kadi].get("active",[])
        if mac in aktif: aktif.remove(mac); db[kadi]["active"]=aktif; db_yaz(db)
    return jsonify({"ok":True})

@app.route("/auth/profil/<kadi>")
def profil(kadi):
    if not auth_bot(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    kadi=kadi.lower(); db=db_oku(); u=db.get(kadi)
    if not u: return jsonify({"ok":False})
    return jsonify({"ok":True,"kadi":kadi,"site":u.get("site",""),"sure":time_left(u.get("expires")),
                    "expires":u.get("expires",""),"max_dev":u.get("max_dev",1),
                    "cihaz_say":len(u.get("macler",[])),"approved":u.get("approved"),"locked":u.get("locked")})

# ── ADMIN KULLANICI ───────────────────────────────────────────
@app.route("/admin/kullanicilar")
def admin_kullanicilar():
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    return jsonify({"ok":True,"kullanicilar":db_oku()})

@app.route("/admin/onayla", methods=["POST"])
def admin_onayla():
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    d=request.json or {}; kadi=d.get("kadi","").lower()
    max_dev=int(d.get("max_dev",1)); gun=int(d.get("gun",30))
    db=db_oku()
    if kadi not in db: return jsonify({"ok":False,"hata":"Bulunamadı."})
    db[kadi]["approved"]=True; db[kadi]["max_dev"]=max_dev
    if gun>0: db[kadi]["expires"]=(datetime.now()+timedelta(days=gun)).isoformat()
    db_yaz(db); return jsonify({"ok":True})

@app.route("/admin/duzenle", methods=["POST"])
def admin_duzenle():
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    d=request.json or {}; kadi=d.get("kadi","").lower()
    max_dev=int(d.get("max_dev",1)); gun=int(d.get("gun",0))
    yon=int(d.get("yon",1)); yeni_pw=d.get("yeni_sifre","").strip()
    db=db_oku()
    if kadi not in db: return jsonify({"ok":False,"hata":"Bulunamadı."})
    db[kadi]["max_dev"]=max_dev
    if gun>0:
        cur=db[kadi].get("expires"); base=parse_iso(cur) if cur else None
        if not base or base<datetime.now(): base=datetime.now()
        yeni=base+timedelta(days=gun*yon)
        if yeni<datetime.now(): yeni=datetime.now()
        db[kadi]["expires"]=yeni.isoformat()
    if yeni_pw and len(yeni_pw)>=6:
        db[kadi]["pw"]=shash(yeni_pw); db[kadi]["pw_plain"]=yeni_pw
    db_yaz(db); return jsonify({"ok":True})

@app.route("/admin/kilitle", methods=["POST"])
def admin_kilitle():
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    d=request.json or {}; kadi=d.get("kadi","").lower(); durum=bool(d.get("kilitle",True))
    db=db_oku()
    if kadi not in db: return jsonify({"ok":False})
    db[kadi]["locked"]=durum
    if durum: db[kadi]["active"]=[]
    db_yaz(db); return jsonify({"ok":True})

@app.route("/admin/sil", methods=["POST"])
def admin_sil():
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    kadi=(request.json or {}).get("kadi","").lower(); db=db_oku()
    if kadi in db:
        kullanici_macleri = db[kadi].get("macler", [])
        if kullanici_macleri:
            dev = dev_oku()
            for mac in kullanici_macleri:
                if mac in dev: del dev[mac]
            dev_yaz(dev)
            with _clk:
                for mac in kullanici_macleri: _cihazlar.pop(mac, None)
        del db[kadi]
        db_yaz(db)
    return jsonify({"ok":True})

@app.route("/admin/reset", methods=["POST"])
def admin_reset():
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    onay = (request.json or {}).get("onay","")
    if onay != "SIFIRLA":
        return jsonify({"ok":False,"hata":"Onay için body'de onay=SIFIRLA gönderin."})
    db_yaz({})
    dev_yaz({})
    with _clk: _cihazlar.clear()
    return jsonify({"ok":True,"mesaj":"Tüm kullanıcı ve cihaz verisi silindi."})

@app.route("/admin/mac-temizle", methods=["POST"])
def admin_mac_temizle():
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    mac = (request.json or {}).get("mac","").strip()
    if not mac: return jsonify({"ok":False,"hata":"mac gerekli"})
    db=db_oku(); dev=dev_oku(); temizlenen=[]
    for k,v in db.items():
        if mac in v.get("macler",[]):
            v["macler"].remove(mac); temizlenen.append(k)
            if mac in v.get("active",[]): v["active"].remove(mac)
    if temizlenen: db_yaz(db)
    if mac in dev: del dev[mac]; dev_yaz(dev)
    with _clk: _cihazlar.pop(mac, None)
    return jsonify({"ok":True,"temizlenen_kullanicilar":temizlenen})

# ── ADMIN CİHAZ ──────────────────────────────────────────────
@app.route("/admin/cihazlar")
def admin_cihazlar():
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    return jsonify({"ok":True,"cihazlar":dev_oku()})

@app.route("/admin/cihaz/engelle", methods=["POST"])
def admin_cihaz_engelle():
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    d=request.json or {}; mac=d.get("mac",""); eng=bool(d.get("engelle",True))
    dev=dev_oku()
    if mac in dev: dev[mac]["engellendi"]=eng; dev_yaz(dev)
    return jsonify({"ok":True})

@app.route("/admin/cihaz/sil", methods=["POST"])
def admin_cihaz_sil():
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    mac=(request.json or {}).get("mac",""); dev=dev_oku()
    if mac in dev: del dev[mac]; dev_yaz(dev)
    return jsonify({"ok":True})

# ── KOMUT ─────────────────────────────────────────────────────
@app.route("/komut/bekle/<mac>", methods=["GET","POST"])
def komut_bekle(mac):
    if not auth_bot(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    dev=dev_oku()
    if dev.get(mac,{}).get("engellendi"): return jsonify({"ok":False,"hata":"Cihaz engellendi."}),403
    simdi=datetime.now().isoformat(); cihaz_al(mac)
    with _clk:
        _cihazlar[mac]["son_gorulme"]=simdi
        if request.json:
            _cihazlar[mac]["hostname"]=request.json.get("hostname","?")
            _cihazlar[mac]["kullanici"]=request.json.get("kullanici","?")
        if _cihazlar[mac]["komutlar"]:
            return jsonify({"ok":True,"komut":_cihazlar[mac]["komutlar"].pop(0)})
    return jsonify({"ok":True,"komut":None})

@app.route("/komut/gonder", methods=["POST"])
def komut_gonder():
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    d=request.json or {}; mac=d.get("mac","").strip(); komut=d.get("komut","").strip()
    if not mac or not komut: return jsonify({"ok":False,"hata":"mac ve komut gerekli"})
    kid=str(uuid.uuid4())[:8]; cihaz_al(mac)
    with _clk: _cihazlar[mac]["komutlar"].append({"id":kid,"cmd":komut,"zaman":datetime.now().isoformat()})
    return jsonify({"ok":True,"id":kid})

@app.route("/komut/sonuc", methods=["POST"])
def komut_sonuc():
    if not auth_bot(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    d=request.json or {}; mac=d.get("mac","").strip()
    if not mac: return jsonify({"ok":False})
    cihaz_al(mac)
    with _clk:
        _cihazlar[mac]["sonuclar"].append({"id":d.get("id",""),"stdout":d.get("stdout",""),
            "stderr":d.get("stderr",""),"returncode":d.get("returncode",-1),"zaman":datetime.now().isoformat()})
        _cihazlar[mac]["sonuclar"]=_cihazlar[mac]["sonuclar"][-50:]
    return jsonify({"ok":True})

@app.route("/komut/oku/<mac>")
def komut_oku(mac):
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    cihaz_al(mac)
    with _clk: sonuclar=list(_cihazlar[mac]["sonuclar"])
    return jsonify({"ok":True,"sonuclar":sonuclar})

@app.route("/komut/liste")
def komut_liste():
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    with _clk:
        liste=[{"mac":mac,"hostname":v["hostname"],"kullanici":v["kullanici"],
                "son_gorulme":v["son_gorulme"],"bekleyen":len(v["komutlar"])} for mac,v in _cihazlar.items()]
    return jsonify({"ok":True,"cihazlar":liste})

# ── TARAMA SONUÇLARI ──────────────────────────────────────────
@app.route("/tarama/sonuc", methods=["POST"])
def tarama_sonuc():
    if not auth_bot(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    d=request.json or {}
    mac=d.get("mac",""); kadi=d.get("kadi","?")
    tarih=d.get("tarih",datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    tid=str(uuid.uuid4())[:8]
    kayit={"id":tid,"mac":mac,"kadi":kadi,"hedef":d.get("hedef",""),"site_adi":d.get("site_adi",""),
           "tarih":tarih,"toplam":d.get("toplam",0),"basarili":d.get("basarili",0),
           "hatali":d.get("hatali",0),"bos":d.get("bos",0)}
    klasor = TARAMA_FILES / tid
    klasor.mkdir(parents=True, exist_ok=True)
    for alan,dosya in [("basarili_txt","basarili.txt"),("hatali_txt","hatali.txt"),("bos_txt","bos.txt")]:
        icerik=d.get(alan,"")
        if icerik: (klasor/dosya).write_text(icerik, encoding="utf-8")
    db=tarama_oku()
    if kadi not in db: db[kadi]=[]
    db[kadi].insert(0,kayit); db[kadi]=db[kadi][:100]
    tarama_yaz(db)
    return jsonify({"ok":True,"id":tid})

@app.route("/admin/taramalar")
def admin_taramalar():
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    kadi=request.args.get("kadi",""); db=tarama_oku()
    if kadi: return jsonify({"ok":True,"taramalar":{kadi:db.get(kadi,[])}})
    return jsonify({"ok":True,"taramalar":db})

@app.route("/admin/tarama/dosya/<tarama_id>/<dosya>")
def admin_tarama_dosya(tarama_id,dosya):
    if not auth_admin(): return jsonify({"ok":False,"hata":"Yetkisiz"}),403
    if dosya not in ("basarili.txt","hatali.txt","bos.txt"):
        return jsonify({"ok":False,"hata":"Geçersiz"}),400
    p=TARAMA_FILES/tarama_id/dosya
    if not p.exists(): return jsonify({"ok":True,"icerik":""})
    return jsonify({"ok":True,"icerik":p.read_text(encoding="utf-8")})

if __name__=="__main__":
    port=int(os.environ.get("PORT",5000))
    app.run(host="0.0.0.0",port=port)