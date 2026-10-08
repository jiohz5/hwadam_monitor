"""상품 탐색, 감시 현황, 연결 설정 화면 구성."""
import tkinter as tk
from tkinter import ttk

from catalog import PRODUCTS
from branding import load_branding
from model import AGE_NAMES, SECTION_NAMES, date_label
from ui_widgets import BG, WHITE, INK, MUTED, GREEN, PALE, LINE, TONES, DateCalendar, TimeGrid, card, palette_styles, tint_card


def heading(parent,title,subtitle=''):
    ttk.Label(parent,text=title,style='Section.TLabel').pack(anchor='w')
    if subtitle: ttk.Label(parent,text=subtitle,style='Muted.Card.TLabel',wraplength=540).pack(anchor='w',pady=(4,12))


def log_view(parent,*,height=8,expanded=False):
    frame=ttk.Frame(parent,style='Card.TFrame');frame.pack(fill='both',expand=True)
    view=tk.Text(frame,height=height,width=1,wrap='word',state='disabled',
        font=('맑은 고딕',11 if expanded else 9),bg='#142F29',fg='#DDECE0',
        selectbackground='#416C53',selectforeground=WHITE,insertbackground='#DDECE0',
        relief='flat',padx=10,pady=8,spacing1=3,spacing3=3)
    scroll=ttk.Scrollbar(frame,command=view.yview,style='Night.Vertical.TScrollbar');scroll.pack(side='right',fill='y')
    view.configure(yscrollcommand=scroll.set);view.pack(fill='both',expand=True)
    return view


def build(app):
    root=app.root
    root.title('HWADAM · 화담숲 예약 모니터')
    app.brand_images=load_branding(root)
    root.geometry(f"1360x{min(920,max(740,root.winfo_screenheight()-80))}")
    root.minsize(1160,740);root.configure(bg=BG)
    style=ttk.Style(root);style.theme_use('clam')
    style.configure('.',font=('맑은 고딕',10),background=BG,foreground=INK)
    style.configure('TFrame',background=BG)
    style.configure('Card.TFrame',background=WHITE)
    style.configure('TLabel',background=BG)
    style.configure('Card.TLabel',background=WHITE)
    style.configure('Section.TLabel',background=WHITE,font=('맑은 고딕',12,'bold'))
    style.configure('Muted.Card.TLabel',background=WHITE,foreground=MUTED,font=('맑은 고딕',9))
    style.configure('Muted.TLabel',foreground=MUTED,font=('맑은 고딕',9))
    style.configure('Title.TLabel',font=('맑은 고딕',21,'bold'))
    style.configure('TButton',background=WHITE,foreground=INK,borderwidth=1,bordercolor=LINE,padding=(13,9),focusthickness=0)
    style.map('TButton',background=[('active',PALE)],foreground=[('disabled','#A9B5AC')])
    style.configure('Accent.TButton',background=GREEN,foreground=WHITE,bordercolor=GREEN,font=('맑은 고딕',11,'bold'),padding=(16,12))
    style.map('Accent.TButton',background=[('disabled','#AABCB0'),('active','#1D503E')],foreground=[('disabled',WHITE),('!disabled',WHITE)])
    style.configure('Quiet.TButton',borderwidth=0,background=WHITE,padding=(8,4))
    style.configure('Small.TButton',font=('맑은 고딕',9),padding=(10,5),borderwidth=0,background='#F1F4EF')
    style.configure('Selected.Small.TButton',background=PALE,foreground=GREEN,font=('맑은 고딕',9,'bold'),padding=(10,5),borderwidth=0)
    style.configure('TEntry',fieldbackground=WHITE,bordercolor=LINE,padding=7)
    style.configure('TCombobox',fieldbackground=WHITE,padding=6,bordercolor=LINE)
    style.configure('TSpinbox',fieldbackground=WHITE,padding=7,bordercolor=LINE)
    style.configure('TRadiobutton',background=WHITE,padding=5)
    style.configure('TCheckbutton',background=WHITE,padding=4)
    style.configure('Treeview',background=WHITE,fieldbackground=WHITE,rowheight=38,borderwidth=0)
    style.configure('Treeview.Heading',background='#EDF2EC',font=('맑은 고딕',10,'bold'),padding=10)
    style.map('Treeview',background=[('selected',PALE)],foreground=[('selected',GREEN)])
    style.configure('Inventory.Treeview',background='#F0F7F1',fieldbackground='#F0F7F1')
    style.configure('Inventory.Treeview.Heading',background='#3D6A54',foreground=WHITE,bordercolor='#3D6A54')
    style.map('Inventory.Treeview.Heading',background=[('active','#4B7B61')])
    style.configure('Night.Vertical.TScrollbar',background='#527660',troughcolor='#142F29',bordercolor='#142F29',arrowcolor='#C8DCCB')
    style.map('Night.Vertical.TScrollbar',background=[('active','#70967A')])
    palette_styles(style)
    sidebar=tk.Frame(root,bg='#173A30',width=218);sidebar.pack(side='left',fill='y');sidebar.pack_propagate(False)
    brand=tk.Frame(sidebar,bg='#173A30');brand.pack(fill='x',padx=22,pady=(16,10))
    if 'sidebar' in app.brand_images:
        tk.Label(brand,image=app.brand_images['sidebar'],bg='#173A30',bd=0).pack()
    else:
        tk.Label(brand,text='화담숲',font=('맑은 고딕',22,'bold'),fg=WHITE,bg='#173A30').pack()
    tk.Label(brand,text='예약 모니터',font=('맑은 고딕',9),fg='#A9BFAF',bg='#173A30').pack(pady=(7,0))
    app.nav_buttons={}
    for name,title in [('explore','◉   홈 · 티켓 탐색'),('monitor','≡   감시 현황'),('payment','▣   결제 준비'),('admin','◇   관리 모드'),('settings','⚙   연결 · 알림')]:
        button=tk.Button(sidebar,text=title,anchor='w',font=('맑은 고딕',11),fg='#DCE5DA',bg='#173A30',
                         activebackground='#2D5544',activeforeground=WHITE,relief='flat',bd=0,padx=18,pady=8,
                         cursor='hand2',command=lambda value=name:app._show_page(value))
        button.pack(fill='x',padx=12,pady=2);app.nav_buttons[name]=button
    tk.Frame(sidebar,bg='#36584A',height=1).pack(fill='x',padx=22,pady=(24,20))
    tk.Label(sidebar,text='상품 보관함',font=('맑은 고딕',9,'bold'),fg='#A9BFAF',bg='#173A30').pack(anchor='w',padx=22,pady=(0,10))
    app.product_buttons={};app.product_stats={}
    for product in PRODUCTS:
        button=tk.Button(sidebar,text=product['name'],wraplength=172,justify='left',anchor='w',
            font=('맑은 고딕',10,'bold'),fg='#F0F4E9',bg='#214638',activebackground='#365B47',activeforeground=WHITE,
            relief='flat',bd=0,padx=15,pady=15,cursor='hand2',command=lambda pid=product['id']:app._choose_product(pid))
        app._condition(button);button.pack(fill='x',padx=14,pady=(5,0));app.product_buttons[product['id']]=button
        var=tk.StringVar(value='회차 목록 미확보');app.product_stats[product['id']]=var
        tk.Label(sidebar,textvariable=var,fg='#AFC2AE',bg='#173A30',font=('맑은 고딕',8)).pack(anchor='w',padx=29,pady=(5,11))
    tk.Label(sidebar,text='원하는 순간을 기다리는\n나만의 예약 도우미',justify='left',font=('맑은 고딕',9),fg='#93AF9C',bg='#173A30').pack(side='bottom',anchor='w',padx=22,pady=26)
    main=ttk.Frame(root,padding=(22,16,20,16));main.pack(side='left',fill='both',expand=True)
    header=tk.Frame(main,bg='#284F42',padx=16,pady=5);header.pack(fill='x',pady=(0,12))
    if 'banner' in app.brand_images:
        tk.Label(header,image=app.brand_images['banner'],bg='#284F42',bd=0).pack(side='right',padx=(10,2))
    tk.Label(header,text='RESERVATION STUDIO',fg='#D9CE97',bg='#284F42',font=('Segoe UI',9,'bold')).pack(anchor='w')
    tk.Label(header,textvariable=app.product_title,fg='#F4F4DF',bg='#284F42',font=('맑은 고딕',19,'bold')).pack(anchor='w',pady=(2,1))
    tk.Label(header,text='원하는 회차를 고르고, 실시간 조회와 알림을 한눈에 확인하세요.',fg='#BED3C2',bg='#284F42',font=('맑은 고딕',9)).pack(anchor='w')
    app.page_area=ttk.Frame(main);app.page_area.pack(fill='both',expand=True)
    app.page_area.columnconfigure(0,weight=1);app.page_area.rowconfigure(0,weight=1)
    app.pages={name:ttk.Frame(app.page_area) for name in ('explore','monitor','settings','payment','admin')}
    for page in app.pages.values(): page.grid(row=0,column=0,sticky='nsew')
    explore=app.pages['explore'];explore.columnconfigure(0,weight=1);explore.columnconfigure(1,weight=0,minsize=318);explore.rowconfigure(1,weight=1)
    toolbar=ttk.Frame(explore);toolbar.grid(row=0,column=0,columnspan=2,sticky='ew',pady=(0,13))
    app.catalog_button=ttk.Button(toolbar,text='↓  두 상품 목록 확보',command=app._sync_catalogues);app.catalog_button.pack(side='left')
    app.refresh_button=ttk.Button(toolbar,text='↻  이 날짜 새로고침',command=lambda:app._load(False));app.refresh_button.pack(side='left',padx=8)
    ttk.Button(toolbar,text='상품 페이지 ↗',style='Quiet.TButton',command=app._open_product).pack(side='right')
    left=ttk.Frame(explore);left.grid(row=1,column=0,sticky='nsew',padx=(0,14))
    app.home_results=card(left,padding=12,accent=TONES['Mint']['accent']);app.home_results.pack(side='bottom',fill='x',pady=(10,0))
    browse=ttk.Frame(left);browse.pack(fill='both',expand=True)
    app.canvas=tk.Canvas(browse,highlightthickness=0,bg=BG,width=575)
    scroll=ttk.Scrollbar(browse,command=app.canvas.yview);scroll.pack(side='right',fill='y')
    app.canvas.pack(side='left',fill='both',expand=True);app.canvas.configure(yscrollcommand=scroll.set)
    app.left_content=ttk.Frame(app.canvas,padding=(0,0,7,0));window=app.canvas.create_window((0,0),window=app.left_content,anchor='nw')
    app.left_content.bind('<Configure>',lambda e:app.canvas.configure(scrollregion=app.canvas.bbox('all')))
    app.canvas.bind('<Configure>',lambda e:app.canvas.itemconfigure(window,width=e.width))
    root.bind('<MouseWheel>',app._wheel)
    modecard=card(app.left_content,padding=12);modecard.pack(fill='x',pady=(0,12))
    for title,value in [('입장권 찾기','entry'),('모노레일 추가구매','addon')]:
        app._condition(ttk.Radiobutton(modecard.content,text=title,variable=app.vars['mode'],value=value,command=app._mode_changed)).pack(side='left',padx=(3,18))
    app.addon_card=card(app.left_content);app.addon_card.pack(fill='x',pady=(0,12))
    heading(app.addon_card.content,'추가구매 주문','입장권 주문에서 연 모노레일 추가구매 주소를 붙여넣으세요.')
    app.addon_entry=app._entry(app.addon_card.content,'addon_url');app.addon_entry.pack(fill='x')
    ttk.Label(app.addon_card.content,textvariable=app.order_label,style='Muted.Card.TLabel',wraplength=490).pack(anchor='w',pady=(8,0))
    datecard=card(app.left_content,padding=12,accent=TONES['Sage']['accent']);datecard.pack(fill='x',pady=(0,10));app.date_card=datecard
    datecard.content.columnconfigure(1,weight=1)
    dateinfo=ttk.Frame(datecard.content,style='Card.TFrame');dateinfo.grid(row=0,column=0,sticky='nw',padx=(0,12),pady=4)
    ttk.Label(dateinfo,text='방문 날짜',style='Section.TLabel').pack(anchor='w',pady=(0,8))
    app.date_combo=app._condition(ttk.Combobox(dateinfo,textvariable=app.vars['date'],width=16));app.date_combo.pack(anchor='w')
    ttk.Label(dateinfo,text='입장권과 모노레일을\n같은 방문일로 확인합니다.',style='Muted.Card.TLabel').pack(anchor='w',pady=(12,0))
    ttk.Label(dateinfo,text='· 회차 목록이 저장된 날짜',style='Muted.Card.TLabel').pack(anchor='w',pady=(8,0))
    app.calendar=DateCalendar(datecard.content,lambda date:app.vars['date'].set(date_label(date)))
    app.calendar.grid(row=0,column=1,sticky='ew')
    controls=ttk.Frame(app.left_content);controls.pack(fill='x',pady=(2,10))
    ttk.Label(controls,text='같은 날짜의 회차 함께 고르기',font=('맑은 고딕',11,'bold')).pack(side='left')
    app.section_combo=app._condition(ttk.Combobox(controls,textvariable=app.vars['section'],values=list(SECTION_NAMES.values()),state='readonly',width=16));app.section_combo.pack(side='right')
    ttk.Label(controls,text='모노레일 구간',style='Muted.TLabel').pack(side='right',padx=(8,8))
    app.round_cards=ttk.Frame(app.left_content);app.round_cards.pack(fill='x',pady=(0,12))
    for column in (0,1): app.round_cards.columnconfigure(column,weight=1,uniform='round-card')
    timecard=card(app.round_cards,padding=12,accent=TONES['Mint']['accent']);timecard.grid(row=0,column=0,sticky='nsew',padx=(0,6));app.primary_card=timecard
    row=ttk.Frame(timecard.content,style='Card.TFrame');row.pack(fill='x')
    ttk.Label(row,textvariable=app.primary_title,style='Section.TLabel').pack(side='left')
    app._condition(ttk.Button(row,text='08:00~08:20',style='Small.TButton',command=app._quick_range)).pack(side='right')
    ttk.Label(timecard.content,textvariable=app.catalog_stamp,style='Muted.Card.TLabel',wraplength=220).pack(anchor='w',pady=(6,10))
    app.primary_list=app._condition(TimeGrid(timecard.content,lambda:app._selected('primary'),height=300));app.primary_list.pack(fill='x')
    ttk.Label(timecard.content,text='선택 시간 · 직접 입력 가능',style='Muted.Card.TLabel').pack(anchor='w',pady=(12,0))
    row=ttk.Frame(timecard.content,style='Card.TFrame');row.pack(fill='x',pady=(10,0))
    app._entry(row,'times',width=10).pack(side='left',fill='x',expand=True)
    app._condition(ttk.Button(row,text='해제',style='Quiet.TButton',command=lambda:app.vars['times'].set(''))).pack(side='right',padx=(5,0))
    monocard=card(app.round_cards,padding=12,accent=TONES['Amber']['accent']);monocard.grid(row=0,column=1,sticky='nsew',padx=(6,0));app.mono_card=monocard
    ttk.Label(monocard.content,text='모노레일 회차',style='Section.TLabel').pack(anchor='w')
    ttk.Label(monocard.content,textvariable=app.mono_stamp,style='Muted.Card.TLabel',wraplength=220).pack(anchor='w',pady=(6,10))
    app.secondary_area=ttk.Frame(monocard.content,style='Card.TFrame');app.secondary_area.pack(fill='x')
    app.secondary_list=app._condition(TimeGrid(app.secondary_area,lambda:app._selected('secondary'),height=300));app.secondary_list.pack(fill='x')
    ttk.Label(app.secondary_area,text='선택 시간 · 여러 회차 체크 가능',style='Muted.Card.TLabel').pack(anchor='w',pady=(12,0))
    row=ttk.Frame(app.secondary_area,style='Card.TFrame');row.pack(fill='x',pady=(10,0))
    app.mono_entry=app._entry(row,'mono_times',width=10);app.mono_entry.pack(side='left',fill='x',expand=True)
    app._condition(ttk.Button(row,text='해제',style='Quiet.TButton',command=lambda:app.vars['mono_times'].set(''))).pack(side='right',padx=(5,0))
    app.mono_check=app._condition(ttk.Checkbutton(monocard.content,text='알림에 모노레일 포함',variable=app.vars['include_monorail'],command=lambda:app._mode_changed(clear=False)));app.mono_check.pack(anchor='w',pady=(10,4))
    app.relative_check=app._condition(ttk.Checkbutton(monocard.content,text='입장 +20분 ±15분으로 찾기',variable=app.vars['mono_relative']));app.relative_check.pack(anchor='w',pady=(0,4))
    ttk.Label(monocard.content,text='시간을 체크하면 알림 대상에 포함됩니다. 미선택 상태에서 포함하면 가까운 후보 8회차를 확인합니다.\n실제 탑승 가능 여부는 선택 입장 회차별로 다시 확인합니다.',style='Muted.Card.TLabel',wraplength=220).pack(anchor='w')
    right=ttk.Frame(explore,width=318);right.grid(row=1,column=1,sticky='nsew');right.columnconfigure(0,weight=1)
    target=card(right,padding=12,accent='#B29455');target.pack(fill='x')
    row=ttk.Frame(target.content,style='Card.TFrame');row.pack(fill='x')
    ttk.Label(row,text='나의 감시 조건',style='Section.TLabel').pack(side='left')
    app.save_button=ttk.Button(row,text='설정 저장',style='Quiet.TButton',command=app._save);app.save_button.pack(side='right')
    ttk.Label(target.content,textvariable=app.date_summary,font=('맑은 고딕',15,'bold'),style='Card.TLabel').pack(anchor='w',pady=(8,3))
    ttk.Label(target.content,textvariable=app.selection_count,foreground=GREEN,style='Card.TLabel').pack(anchor='w')
    tk.Label(target.content,textvariable=app.selection_summary,wraplength=278,bg=WHITE,fg=MUTED,font=('맑은 고딕',9),height=2,anchor='nw',justify='left').pack(anchor='w',pady=(4,6))
    ttk.Separator(target.content).pack(fill='x',pady=(0,8))
    quantities=ttk.Frame(target.content,style='Card.TFrame');quantities.pack(fill='x')
    for i,(age,label) in enumerate(AGE_NAMES.items()):
        cell=ttk.Frame(quantities,style='Card.TFrame');cell.grid(row=0,column=i,sticky='ew',padx=(0,6),pady=(0,4));quantities.columnconfigure(i,weight=1)
        ttk.Label(cell,text=label,style='Muted.Card.TLabel').pack(anchor='w',pady=(0,3))
        app._condition(ttk.Spinbox(cell,textvariable=app.counts[age],from_=0,to=999,width=4)).pack(fill='x')
    tip=tk.Label(target.content,text='선택 권종에 1장만 생겨도 알림',bg=PALE,fg=GREEN,font=('맑은 고딕',9,'bold'),padx=8,pady=7);tip.pack(fill='x',pady=(4,8))
    row=ttk.Frame(target.content,style='Card.TFrame');row.pack(fill='x')
    app.start_button=ttk.Button(row,text='감시 시작',style='Accent.TButton',command=app._start);app.start_button.pack(side='left',fill='x',expand=True)
    app.stop_button=ttk.Button(row,text='중지',state='disabled',width=5,command=app._stop);app.stop_button.pack(side='left',fill='y',padx=(6,0))
    statuscard=card(right,padding=12,accent=TONES['Night']['accent']);statuscard.pack(fill='both',expand=True,pady=(10,0));app.home_log_card=statuscard
    row=ttk.Frame(statuscard.content,style='Card.TFrame');row.pack(fill='x')
    ttk.Label(row,text='실시간 로그',style='Section.TLabel').pack(side='left')
    ttk.Button(row,text='크게 보기 ↗',style='Quiet.TButton',command=lambda:app._show_page('monitor')).pack(side='right')
    ttk.Label(statuscard.content,textvariable=app.status,wraplength=280,foreground=GREEN,style='Card.TLabel').pack(anchor='w',pady=(6,4))
    ttk.Label(statuscard.content,textvariable=app.session_status,wraplength=280,style='Muted.Card.TLabel').pack(anchor='w')
    ttk.Frame(statuscard.content,style='Card.TFrame',height=8).pack()
    app.home_log_text=log_view(statuscard.content,height=5)
    monitor=app.pages['monitor']
    resultcard=app.home_results
    row=ttk.Frame(resultcard.content,style='Card.TFrame');row.pack(fill='x')
    ttk.Label(row,text='회차별 현재 재고',style='Section.TLabel').pack(side='left')
    ttk.Label(resultcard.content,textvariable=app.detail,style='Muted.Card.TLabel',wraplength=620).pack(anchor='w',pady=(3,5))
    tableframe=ttk.Frame(resultcard.content,style='Card.TFrame');tableframe.pack(fill='both',expand=True)
    app.table=ttk.Treeview(tableframe,columns=('type','time','stock','ages'),show='headings',height=2,style='Inventory.Treeview')
    for key,label,width in [('type','티켓',65),('time','시각',60),('stock','가용 상태',220),('ages','가능 권종',120)]:
        app.table.heading(key,text=label);app.table.column(key,width=width,minwidth=60)
    tablescroll=ttk.Scrollbar(tableframe,command=app.table.yview);tablescroll.pack(side='right',fill='y');app.table.configure(yscrollcommand=tablescroll.set)
    app.table.pack(fill='both',expand=True)
    app.extra_label=tk.StringVar(value='감시를 시작하면 최신 재고를 확인합니다.')
    ttk.Label(resultcard.content,textvariable=app.extra_label,style='Muted.Card.TLabel',wraplength=620).pack(anchor='w',pady=(5,0))
    logcard=card(monitor,padding=14,accent=TONES['Night']['accent']);logcard.pack(fill='both',expand=True)
    row=ttk.Frame(logcard.content,style='Card.TFrame');row.pack(fill='x',pady=(0,8))
    ttk.Label(row,text='감시 현황 · 전체 로그',style='Section.TLabel').pack(side='left')
    ttk.Button(row,text='← 홈으로',style='Quiet.TButton',command=lambda:app._show_page('explore')).pack(side='right')
    ttk.Label(logcard.content,textvariable=app.status,foreground=GREEN,style='Card.TLabel').pack(anchor='w',pady=(0,8))
    app.log_text=log_view(logcard.content,expanded=True)
    settings=app.pages['settings']
    session=card(settings);session.pack(fill='x',pady=(0,14))
    heading(session.content,'NOL 계정 연결','전용 브라우저에서 로그인한 뒤 세션을 저장하면 다음 연결에도 사용할 수 있습니다.')
    row=ttk.Frame(session.content,style='Card.TFrame');row.pack(fill='x')
    app.login_button=ttk.Button(row,text='로그인 브라우저 열기',command=app._open_login);app.login_button.pack(side='left',padx=(0,8))
    app.session_button=ttk.Button(row,text='로그인 세션 저장',command=app._save_session);app.session_button.pack(side='left',padx=(0,8))
    app.connect_button=ttk.Button(row,text='브라우저 연결·목록 읽기',command=lambda:app._load(True));app.connect_button.pack(side='left')
    ttk.Label(session.content,textvariable=app.session_status,style='Muted.Card.TLabel').pack(anchor='w',pady=(10,0))
    tg=card(settings);tg.pack(fill='x',pady=(0,14));heading(tg.content,'텔레그램 알림','가용 티켓이 발견되면 구매 링크와 확인한 재고를 보냅니다.')
    ttk.Label(tg.content,text='봇 토큰',style='Muted.Card.TLabel').pack(anchor='w');app._entry(tg.content,'token',show='●').pack(fill='x',pady=(4,10))
    row=ttk.Frame(tg.content,style='Card.TFrame');row.pack(fill='x')
    ttk.Label(row,text='채팅 ID',style='Card.TLabel').pack(side='left',padx=(0,10));app._entry(row,'chat',width=32).pack(side='left',fill='x',expand=True)
    app.test_button=ttk.Button(row,text='알림 테스트',command=app._test_telegram);app.test_button.pack(side='left',padx=(10,0))
    advanced=card(settings);advanced.pack(fill='x')
    heading(advanced.content,'조회 설정')
    row=ttk.Frame(advanced.content,style='Card.TFrame');row.pack(fill='x',pady=(12,10))
    for title,key in [('조회 주기(초)','interval'),('반복 알림(초)','repeat')]:
        ttk.Label(row,text=title,style='Card.TLabel').pack(side='left',padx=(0,8));app._entry(row,key,width=7).pack(side='left',padx=(0,22))
    ttk.Label(advanced.content,text='반복 알림 0초: 첫 발견·재등장·재고 증가 등 변화가 있을 때만 알림',style='Muted.Card.TLabel').pack(anchor='w',pady=(0,12))
    ttk.Label(advanced.content,text='선택 상품 주소',style='Muted.Card.TLabel').pack(anchor='w');app._entry(advanced.content,'product_url').pack(fill='x',pady=(4,0))
    from ui_operations import OperationsUI
    app.operations=OperationsUI(app)
    for widget,tone in ((modecard,'Cream'),(app.addon_card,'Amber'),(datecard,'Sage'),
                        (timecard,'Mint'),(monocard,'Amber'),(target,'Cream'),
                        (resultcard,'Mint'),(statuscard,'Night'),(logcard,'Night'),
                        (session,'Sage'),(tg,'Mint'),(advanced,'Cream')):
        tint_card(widget,tone)
    app._show_page('explore')
