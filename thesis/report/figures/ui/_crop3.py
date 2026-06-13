from PIL import Image
def crop(src, box, out, scale=2):
    im = Image.open(src)
    c = im.crop(box).convert('RGB')
    c = c.resize((c.width*scale, c.height*scale), Image.LANCZOS)
    c.save(out)
    print(out, c.size)
# Engine card middle bands (sparkline + metric text)
crop('Engines.png', (245, 250, 760, 345), '_crops/e1_mid.png', 3)
crop('Engines.png', (245, 435, 760, 530), '_crops/e2_mid.png', 3)
# Service Health right columns (response time / status)
crop('Home.png', (640, 230, 905, 470), '_crops/h_service_cols.png', 3)
# Remaining top cards (Requests, Policy compliance)
crop('Home.png', (700, 95, 1135, 165), '_crops/h_topcards2.png', 2)
# Engines top summary cards
crop('Engines.png', (230, 95, 1135, 175), '_crops/e_topcards.png', 2)
