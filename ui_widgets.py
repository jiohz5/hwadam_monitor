"""달력과 체크형 회차 탐색 위젯."""
import calendar
import datetime as dt
import tkinter as tk
from tkinter import ttk

BG='#EDEFE7'
WHITE='#FFFFFF'
INK='#203C34'
MUTED='#738078'
GREEN='#25644D'
PALE='#EAF3ED'
LINE='#E1E7DF'
TONES={
    'Paper':dict(bg=WHITE,ink=INK,muted=MUTED,accent=GREEN,line=LINE,selected='#C7E3D3'),
    'Sage':dict(bg='#E4ECD9',ink='#2B4736',muted='#65785D',accent='#4D7147',line='#C5D5B7',selected='#CBDDBD'),
    'Mint':dict(bg='#E7F2EB',ink='#224B3B',muted='#5E7C6B',accent='#267359',line='#BDDBCA',selected='#BFE0CD'),
    'Amber':dict(bg='#FAEED8',ink='#604726',muted='#8C7250',accent='#956126',line='#E9CCA0',selected='#EFD29E'),
    'Cream':dict(bg='#FAF5E8',ink='#3F4A36',muted='#7A8068',accent='#4C7050',line='#DDDBC0',selected='#E4E8CB'),
    'Night':dict(bg='#203F37',ink='#E3EFE5',muted='#A4C2B3',accent='#B5D59D',line='#34574B',selected='#355C4A'),
}


def palette_styles(style):
    for name,p in TONES.items():
        style.configure(name+'.Card.TFrame',background=p['bg'])
        for suffix,color in (('Card.TLabel',p['ink']),('Section.TLabel',p['accent']),('Muted.Card.TLabel',p['muted'])):
            style.configure(name+'.'+suffix,background=p['bg'],foreground=color)
        for suffix in ('TCheckbutton','TRadiobutton'):
            style.configure(name+'.'+suffix,background=p['bg'],foreground=p['ink'])
            style.map(name+'.'+suffix,background=[('active',p['selected'])],foreground=[('disabled',p['muted'])])
        for suffix in ('Quiet.TButton','Small.TButton'):
            style.configure(name+'.'+suffix,background=p['bg'],foreground=p['accent'])
            style.map(name+'.'+suffix,background=[('active',p['selected'])])
        style.configure(name+'.Selected.Small.TButton',background=p['accent'],foreground=WHITE)
        style.map(name+'.Selected.Small.TButton',background=[('active',p['accent'])],foreground=[('!disabled',WHITE)])


def tint_surface(widget,tone):
    """카드 안의 정적 위젯과 다시 그려지는 날짜·회차에 같은 팔레트를 적용한다."""
    p=TONES[tone]
    if isinstance(widget,(DateCalendar,TimeGrid)): widget.tone=tone
    if isinstance(widget,ttk.Frame): widget.configure(style=tone+'.Card.TFrame')
    elif isinstance(widget,ttk.Label):
        base=str(widget.cget('style')) or 'Card.TLabel'
        base='Muted.Card.TLabel' if 'Muted' in base else 'Section.TLabel' if 'Section' in base else 'Card.TLabel'
        widget.configure(style=tone+'.'+base,foreground=p['muted'] if 'Muted' in base else p['accent'] if 'Section' in base else p['ink'])
    elif isinstance(widget,(ttk.Checkbutton,ttk.Radiobutton)):
        widget.configure(style=tone+'.'+widget.winfo_class())
    elif isinstance(widget,ttk.Button) and str(widget.cget('style')) in ('Quiet.TButton','Small.TButton'):
        widget.configure(style=tone+'.'+str(widget.cget('style')))
    elif isinstance(widget,tk.Label) and widget.cget('bg')==WHITE:
        widget.configure(bg=p['bg'],fg=p['muted'] if widget.cget('fg')==MUTED else p['ink'])
    for child in widget.winfo_children(): tint_surface(child,tone)
    if isinstance(widget,TimeGrid): widget.canvas.configure(bg=p['bg']);widget.render()
    elif isinstance(widget,DateCalendar): widget.render()


def tint_card(widget,tone):
    p=TONES[tone]
    widget.configure(bg=p['bg'],highlightbackground=p['line'])
    tint_surface(widget.content,tone)


def card(parent,*,padding=18,accent=None):
    border=tk.Frame(parent,bg=WHITE,highlightbackground=LINE,highlightthickness=1,bd=0)
    if accent: tk.Frame(border,bg=accent,height=3).pack(fill='x')
    inner=ttk.Frame(border,style='Card.TFrame',padding=padding)
    inner.pack(fill='both',expand=True)
    border.content=inner
    return border


class DateCalendar(ttk.Frame):
    def __init__(self,parent,command):
        super().__init__(parent,style='Card.TFrame')
        self.command=command
        self.tone='Paper'
        self.selected='2026-10-25';self.month=(2026,10)
        self.start='';self.end='';self.dates=set();self.saved=set();self.disabled=False
        header=ttk.Frame(self,style='Card.TFrame');header.pack(fill='x',pady=(0,4))
        self.previous=ttk.Button(header,text='‹',width=3,style='Quiet.TButton',command=lambda:self._move(-1));self.previous.pack(side='left')
        self.title=tk.StringVar();ttk.Label(header,textvariable=self.title,style='Section.TLabel',anchor='center').pack(side='left',fill='x',expand=True)
        self.next=ttk.Button(header,text='›',width=3,style='Quiet.TButton',command=lambda:self._move(1));self.next.pack(side='right')
        self.grid_frame=ttk.Frame(self,style='Card.TFrame');self.grid_frame.pack(fill='x')

    def set_context(self,product,selected):
        try: value=dt.date.fromisoformat(selected)
        except (ValueError,TypeError): return
        self.selected=selected;self.month=(value.year,value.month)
        self.start=product.get('start','');self.end=product.get('end','')
        self.dates=set(product.get('dates') or [])
        self.saved={day for day,records in product.get('days',{}).items() if 'entry' in records}
        self.render()

    def _move(self,delta):
        if self.disabled: return
        value=self.month[0]*12+self.month[1]-1+delta
        self.month=(value//12,value%12+1);self.render()

    def set_enabled(self,enabled):
        self.disabled=not enabled;self.render()

    def render(self):
        p=TONES[self.tone]
        for widget in self.grid_frame.winfo_children(): widget.destroy()
        year,month=self.month;self.title.set(f'{year}년 {month}월')
        for column,name in enumerate('일월화수목금토'):
            self.grid_frame.columnconfigure(column,weight=1,uniform='day')
            color='#BB6C63' if column==0 else '#6589A5' if column==6 else MUTED
            ttk.Label(self.grid_frame,text=name,foreground=color,anchor='center',style=self.tone+'.Muted.Card.TLabel').grid(row=0,column=column,sticky='ew',pady=(0,3))
        for row,week in enumerate(calendar.Calendar(firstweekday=6).monthdayscalendar(year,month),1):
            for column,day in enumerate(week):
                if not day: continue
                date=f'{year:04d}-{month:02d}-{day:02d}'
                permitted=not self.disabled and (not self.start or date>=self.start) and (not self.end or date<=self.end) and (not self.dates or date in self.dates)
                chosen=date==self.selected
                text=str(day)+('  ·' if date in self.saved else '')
                color='#BB6C63' if column==0 else '#6589A5' if column==6 else INK
                button=tk.Button(self.grid_frame,text=text,font=('맑은 고딕',9,'bold' if chosen else 'normal'),
                    bg=p['accent'] if chosen else p['bg'],fg=WHITE if chosen else color,disabledforeground=WHITE if chosen else '#889581',
                    activebackground=p['selected'],activeforeground=p['accent'],relief='flat',bd=0,pady=2,cursor='hand2',
                    state='normal' if permitted else 'disabled',command=lambda value=date:self.command(value))
                button.grid(row=row,column=column,sticky='ew',padx=2,pady=1)
        first=f'{year:04d}-{month:02d}-01';last=f'{year:04d}-{month:02d}-{calendar.monthrange(year,month)[1]:02d}'
        self.previous.configure(state='normal' if not self.disabled and (not self.start or first>self.start) else 'disabled')
        self.next.configure(state='normal' if not self.disabled and (not self.end or last<self.end) else 'disabled')


class TimeGrid(ttk.Frame):
    """품절 여부와 무관하게 감시 회차를 체크한다. 상태 색상은 선택만 나타낸다."""
    def __init__(self,parent,command,*,height=230):
        super().__init__(parent,style='Card.TFrame')
        self.command=command;self.items=[];self.selected=set();self.buttons={};self.check_vars={};self.disabled=False
        self.tone='Paper'
        self.filter='전체';self.columns=5;self.max_height=height
        bar=ttk.Frame(self,style='Card.TFrame');bar.pack(fill='x',pady=(0,8))
        self.filter_buttons={}
        for name in ('전체','오전','오후'):
            button=ttk.Button(bar,text=name,style='Small.TButton',command=lambda value=name:self.set_filter(value))
            button.pack(side='left',padx=(0,6));self.filter_buttons[name]=button
        self.canvas=tk.Canvas(self,bg=WHITE,highlightthickness=0,height=70,width=160)
        scroll=ttk.Scrollbar(self,orient='vertical',command=self.canvas.yview)
        scroll.pack(side='right',fill='y');self.canvas.pack(fill='both',expand=True)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.content=ttk.Frame(self.canvas,style='Card.TFrame')
        self.window=self.canvas.create_window((0,0),window=self.content,anchor='nw')
        self.content.bind('<Configure>',lambda e:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',self._resize)

    def _resize(self,event):
        self.canvas.itemconfigure(self.window,width=event.width)
        columns=max(1,min(6,event.width//105))
        if columns!=self.columns: self.columns=columns;self.render()

    def set_filter(self,value): self.filter=value;self.render()

    def set_rows(self,rows,selected=()):
        self.items=[(row['time'],row.get('roundStatusCode')) for row in rows]
        self.selected={time for time in selected if any(item[0]==time for item in self.items)}
        self.render()

    def toggle(self,time):
        if self.disabled: return
        if time in self.selected: self.selected.remove(time)
        else: self.selected.add(time)
        self.render();self.command()

    def render(self):
        p=TONES[self.tone]
        for child in self.content.winfo_children(): child.destroy()
        self.buttons={};self.check_vars={}
        shown=[item for item in self.items if self.filter=='전체' or (item[0]<'12:00')==(self.filter=='오전')]
        for i in range(6): self.content.columnconfigure(i,weight=1 if i<self.columns else 0,uniform='time' if i<self.columns else '')
        for index,(time,status) in enumerate(shown):
            selected=time in self.selected
            checked=tk.BooleanVar(master=self,value=selected);self.check_vars[time]=checked
            button=tk.Checkbutton(self.content,text=time,variable=checked,indicatoron=True,
                 font=('맑은 고딕',10,'bold' if selected else 'normal'),anchor='w',
                 bg=p['selected'] if selected else p['bg'],fg=p['accent'] if selected else p['ink'],activebackground=p['selected'],
                 disabledforeground=p['accent'] if selected else p['muted'],
                 highlightthickness=1,highlightbackground=p['accent'] if selected else p['line'],
                 selectcolor=WHITE,activeforeground=p['accent'],relief='flat',bd=0,pady=8,padx=5,cursor='hand2',
                 state='disabled' if self.disabled else 'normal',command=lambda value=time:self.toggle(value))
            button.grid(row=index//self.columns,column=index%self.columns,sticky='ew',padx=3,pady=3)
            self.buttons[time]=button
        if not shown:
            text='먼저 이 날짜의 회차 목록을 확보해 주세요.' if not self.items else '이 시간대에는 회차가 없습니다.'
            ttk.Label(self.content,text=text,style=self.tone+'.Muted.Card.TLabel',anchor='center',wraplength=180,padding=(4,20)).grid(row=0,column=0,columnspan=self.columns,sticky='ew')
        rows=(len(shown)+self.columns-1)//self.columns
        self.canvas.configure(height=min(self.max_height,max(64,rows*54)))
        for name,button in self.filter_buttons.items(): button.configure(style=self.tone+('.Selected.Small.TButton' if name==self.filter else '.Small.TButton'))

    def cget(self,key):
        if key=='state': return 'disabled' if self.disabled else 'normal'
        return super().cget(key)

    def configure(self,cnf=None,**kwargs):
        if 'state' in kwargs:
            self.disabled=kwargs.pop('state')=='disabled'
            self.render()
        if cnf or kwargs: return super().configure(cnf,**kwargs)
    config=configure

    def delete(self,*args): self.items=[];self.selected=set();self.render()
    def curselection(self): return tuple(i for i,item in enumerate(self.items) if item[0] in self.selected)
    def selection_clear(self,*args): self.selected.clear();self.render()
    def selection_set(self,index): self.selected.add(self.items[index][0]);self.render()
    def scroll_units(self,delta): self.canvas.yview_scroll(delta,'units')
