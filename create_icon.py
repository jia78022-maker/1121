"""生成客户管理器的 Windows 图标：客户档案 + 交期勾选。"""
from pathlib import Path
from PIL import Image, ImageDraw


SIZE = 1024
image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)

# 深蓝圆角底板，适配 Windows 浅色与深色桌面。
draw.rounded_rectangle((52, 52, 972, 972), radius=220, fill="#16233B")
draw.rounded_rectangle((74, 74, 950, 950), radius=198, outline="#31507E", width=12)

# 档案卡片。
draw.rounded_rectangle((220, 210, 804, 814), radius=92, fill="#F7FAFF")
draw.rounded_rectangle((220, 210, 804, 814), radius=92, outline="#A9C7FF", width=18)
draw.rounded_rectangle((220, 210, 804, 340), radius=90, fill="#4B8DFF")
draw.rectangle((220, 286, 804, 340), fill="#4B8DFF")

# 客户头像与资料行。
draw.ellipse((326, 400, 464, 538), fill="#17233B")
draw.rounded_rectangle((292, 556, 500, 664), radius=54, fill="#17233B")
for y, length in ((430, 148), (500, 198), (570, 174)):
    draw.rounded_rectangle((548, y, 548 + length, y + 28), radius=14, fill="#B8C9E8")

# 交期确认：明亮的蓝色圆圈和白色勾选。
draw.ellipse((544, 624, 748, 828), fill="#2563EB")
draw.line((590, 724, 640, 770, 710, 678), fill="white", width=32, joint="curve")

output = Path(__file__).with_name("客户管理器.ico")
image.save(output, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
image.save(Path(__file__).with_name("客户管理器图标.png"), format="PNG")
print(output)
