from PIL import Image
def crop(src, box, out, scale=3):
    im = Image.open(src)
    c = im.crop(box).convert('RGB')
    c = c.resize((c.width*scale, c.height*scale), Image.LANCZOS)
    c.save(out)
    print(out, c.size)
# Full width of each engine card incl right-side chips
crop('Engines.png', (245, 200, 1135, 340), '_crops/e1_full.png', 2)
crop('Engines.png', (245, 385, 1135, 525), '_crops/e2_full.png', 2)
crop('Engines.png', (245, 565, 1135, 730), '_crops/e3_full.png', 2)
# Home: Platform Health card (left) and Service Health (middle)
crop('Home.png', (18, 175, 470, 470), '_crops/h_platform.png', 2)
crop('Home.png', (480, 175, 900, 470), '_crops/h_service.png', 2)
# Home top stat cards
crop('Home.png', (18, 95, 1135, 165), '_crops/h_topcards.png', 2)
