"""프로그램에 포함한 화담숲 공식 로고와 창 아이콘."""
from pathlib import Path
import tkinter as tk


ASSETS=Path(__file__).resolve().parent/'assets'/'branding'


def load_branding(root):
    # PhotoImage는 참조를 유지해야 화면에서 사라지지 않는다.
    images={}
    try:
        logo=tk.PhotoImage(master=root,file=str(ASSETS/'hwadam-logo-white.png'))
        images['sidebar']=logo.subsample(4)
        images['banner']=logo.subsample(6)
    except tk.TclError:
        pass  # 이미지가 빠진 배포본도 텍스트 브랜드로 실행할 수 있다.
    icons=[]
    for size in (192,96,32,16):
        try:
            icon=tk.PhotoImage(master=root,file=str(ASSETS/f'hwadam-{size}.png'))
        except tk.TclError:
            continue
        images[f'icon_{size}']=icon
        icons.append(icon)
    if icons:
        root.iconphoto(True,*icons)
        root.iconphoto(False,*icons)
    return images
