from PIL import Image
def crop(src, box, out, scale=2):
    im = Image.open(src)
    c = im.crop(box).convert('RGB')
    c = c.resize((c.width*scale, c.height*scale), Image.LANCZOS)
    c.save(out)
    print(out, c.size)
crop('Engines.png', (230, 175, 1130, 360), '_crops/eng_card1.png', 2)
crop('Engines.png', (230, 360, 1130, 545), '_crops/eng_card2.png', 2)
crop('Engines.png', (230, 545, 1130, 745), '_crops/eng_card3.png', 2)
