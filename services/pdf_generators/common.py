from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle

WINE = colors.HexColor("#7B1D34")
WINE_LIGHT = colors.HexColor("#F7EEF1")
SLATE = colors.HexColor("#3D3D4E")
GREY = colors.HexColor("#78788C")
RULE = colors.HexColor("#DDD0D5")
ROW_ALT = colors.HexColor("#FAF5F7")
TABLE_HEAD_BG = colors.HexColor("#EDE0E5")
WHITE = colors.white
NEG_RED = colors.HexColor("#C0392B")
POS_GREEN = colors.HexColor("#1E7B4B")
TEAL = colors.HexColor("#0f766e")
GRID = colors.HexColor("#d1d5db")

PAGE_WIDTH, PAGE_HEIGHT = A4


def make_style(name, **kwargs):
    defaults = dict(fontName="Helvetica", fontSize=9, textColor=SLATE, leading=13)
    defaults.update(kwargs)
    return ParagraphStyle(name, **defaults)
