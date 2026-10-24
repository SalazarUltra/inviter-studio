"""Generate the geometric Inviter logo and native Windows installer artwork."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def create_assets(folder: Path):
    folder.mkdir(parents=True, exist_ok=True)
    logo = Image.new("RGBA", (256, 256))
    draw = ImageDraw.Draw(logo)
    draw.rounded_rectangle((8, 8, 248, 248), radius=52, fill="#D5FF00")
    draw.rounded_rectangle((116, 67, 140, 189), radius=4, fill="#111315")
    logo.save(folder / "app.ico", sizes=[(size, size) for size in (16, 24, 32, 48, 64, 128, 256)])
    small = Image.new("RGB", (110, 110), "#D5FF00")
    resized = logo.resize((110, 110))
    small.paste(resized, mask=resized.getchannel("A"))
    small.save(folder / "small.bmp")
    banner = Image.new("RGB", (328, 628), "#111315")
    resized = logo.resize((180, 180))
    banner.paste(resized, (50, 70), resized.getchannel("A"))
    draw = ImageDraw.Draw(banner)
    font_path = Path("C:/Windows/Fonts/segoeuib.ttf")
    font = ImageFont.truetype(str(font_path), 38) if font_path.is_file() else ImageFont.load_default()
    draw.multiline_text((50, 290), "INVITER\nSTUDIO", font=font, fill="#F5F6F7", spacing=4)
    draw.rectangle((50, 480, 270, 484), fill="#D5FF00")
    banner.save(folder / "wizard.bmp")
