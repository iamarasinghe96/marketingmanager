from PIL import Image,ImageDraw
from bot.config import ROOT


def main():
    img = Image.new("RGBA",(256,256),"#0F568C")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((52,62,204,194),radius=16,fill="white")
    draw.rectangle((52,86,204,99),fill="#5CD5A8")
    for x in (84,120,156):
        draw.ellipse((x,120,x+14,134),fill="#0F568C")
    draw.line((88,163,114,184,169,140),fill="#0F568C",width=12)
    img.save(ROOT/"assets"/"marketing-manager.ico",sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])


if __name__ == "__main__":
    main()
