"""Read-only check: which states run the MyLicense *bulk* verification module?
One confirmed module = one adapter that covers every state that has it."""
import requests, re, sys
UA=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) LHAIV-source-recon/1.0 "
    "(read-only licensing-data feasibility check)")
STATES=["alabama","alaska","arizona","arkansas","california","colorado","connecticut",
 "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa","kansas",
 "kentucky","louisiana","maine","maryland","massachusetts","michigan","minnesota",
 "mississippi","missouri","montana","nebraska","nevada","newhampshire","newjersey",
 "newmexico","newyork","northcarolina","northdakota","ohio","oklahoma","oregon",
 "pennsylvania","rhodeisland","southcarolina","southdakota","tennessee","texas","utah",
 "vermont","virginia","washington","westvirginia","wisconsin","wyoming"]
def check(host, path):
    try:
        r=requests.get(f"https://{host}{path}",headers={"User-Agent":UA},timeout=12,allow_redirects=True)
        return r.status_code, len(r.text), r.text
    except Exception as e:
        return None, 0, f"{type(e).__name__}"
print(f"{'state':<15} {'bulk':<12} {'plain-verif':<12} notes")
for s in STATES:
    host=f"{s}.mylicense.com"
    sc,ln,body = check(host,"/Verification_Bulk/")
    sc2,ln2,_  = check(host,"/Verification/")
    bulk = "YES" if (sc==200 and ln>800) else (str(sc) if sc else "no-dns")
    plain= "YES" if (sc2==200 and ln2>800) else (str(sc2) if sc2 else "no-dns")
    note=""
    if bulk=="YES":
        note = "CAPTCHA" if re.search(r"(recaptcha/api\.js|hcaptcha|turnstile)",body,re.I) else "no-captcha"
    if bulk!="no-dns" or plain!="no-dns":
        print(f"{s:<15} {bulk:<12} {plain:<12} {note}")
