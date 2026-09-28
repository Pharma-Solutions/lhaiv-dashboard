import re, sys, requests
UA=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) LHAIV-source-recon/1.0 "
    "(read-only licensing-data feasibility check)")
RX=re.compile(r"(recaptcha|g-recaptcha|grecaptcha|hcaptcha|captcha|cf-turnstile|are you (a )?human|bot detection)",re.I)
for url in sys.argv[1:]:
    print("="*90); print(url)
    try:
        r=requests.get(url,headers={"User-Agent":UA},timeout=30,allow_redirects=True)
        h=r.text
        print(f"  status={r.status_code} final={r.url[:90]} len={len(h)}")
        hits=[]
        for m in RX.finditer(h):
            s=max(0,m.start()-90); hits.append(h[s:m.end()+90].replace("\n"," ").replace("\r"," "))
        print(f"  CAPTCHA regex hits: {len(hits)}")
        for x in hits[:6]: print("    ...",re.sub(r"\s+"," ",x)[:200])
        # real captcha infrastructure?
        real=re.search(r"(recaptcha/api\.js|hcaptcha\.com/1|challenges\.cloudflare\.com/turnstile|www\.google\.com/recaptcha)",h,re.I)
        print("  REAL captcha script/iframe:", real.group(0) if real else "NONE")
    except Exception as e:
        print("  ERROR", type(e).__name__, e)
