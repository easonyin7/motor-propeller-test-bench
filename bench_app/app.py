"""Chinese Windows desktop application. Startup never connects or spins a motor."""
import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog,messagebox,ttk

from .model import CAMERA_CONFIG,CAMPAIGN,DATA,LEVELS,MAIN_TEMPLATE,MAX_LEVEL,MIN_LEVEL,ROOT,TEMPLATES,Plan,Step
from .data import export_session,load_campaign,number,read_json,supplement_levels,write_json
from .controller import Controller,scan_ports

RUNTIME=ROOT/'bench_app/runtime'
RUNTIME.mkdir(parents=True,exist_ok=True)
os.environ.setdefault('MPLCONFIGDIR',str(RUNTIME/'matplotlib'))
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib import rcParams
from PIL import Image,ImageOps,ImageTk

rcParams.update({'font.sans-serif':['Microsoft YaHei','SimHei','DejaVu Sans'],
                 'axes.unicode_minus':False,'font.size':9,'axes.spines.top':False,
                 'axes.spines.right':False,'axes.edgecolor':'#CBD5E1','text.color':'#334155',
                 'axes.labelcolor':'#475569','xtick.color':'#64748B','ytick.color':'#64748B'})
BG='#F1F5F9';NAV='#132737';TEXT='#142B3A';MUTED='#64748B';TEAL='#087E8B';RED='#C43838'
STATUS={'preparing':'准备中','preparation_passed_no_motor_commands':'预检通过 · 电机未启动',
        'demo_complete':'历史回放完成','cancelled':'回放已停止','aborted':'已中止',
        'software_complete_waiting_operator':'软件已停机 · 等待现场确认',
        'aborted_waiting_operator':'保护中止 · 等待现场确认',
        'cancelled_waiting_operator':'停止请求已结束 · 等待现场确认',
        'resuming':'续测准备中',
        'software_complete_operator_confirmed':'测试完成 · 现场已确认停转',
        'aborted_operator_confirmed':'已中止 · 现场已确认停转',
        'cancelled_operator_confirmed':'已停止 · 现场已确认停转'}
STATUS.update(storage_error='记录保存失败',storage_error_waiting_operator='记录保存失败 · 等待现场停转确认',
              storage_error_operator_confirmed='记录保存失败 · 现场已确认停转')

def text_number(value,places=2):
    try:return f'{float(value):.{places}f}' if math.isfinite(float(value)) else '—'
    except (ValueError,TypeError):return '—'

def draw_curves(figure,points,reference=()):
    figure.clear()
    axes=figure.subplots(2,3)
    # The first panel uses measured thrust as the x axis, as required for the
    # 300..500 g refinement. The other four keep throttle on the x axis.
    panels=[('thrust_power','输入功率 / W','拉力 / g',TEAL),
            ('thrust_g','拉力 / g','输出指令 / %',TEAL),
            ('current_a','电流 / A','输出指令 / %','#377DCC'),
            ('power_w','输入功率 / W','输出指令 / %','#865ABC'),
            ('efficiency','拉力效率 / (g/W)','输出指令 / %','#BF7C1C')]
    def coordinates(p,key):
        metrics=p.get('metrics') or {}
        if key=='thrust_power':
            thrust=(metrics.get('thrust_g') or {}).get('mean')
            power=(metrics.get('power_w') or {}).get('mean')
            return None if thrust is None or power is None else (float(thrust),float(power))
        value=(p.get('efficiency_g_per_w') if key=='efficiency'
               else (metrics.get(key) or {}).get('mean'))
        return None if value is None else (float(p['level'])*100,float(value))
    def series(ax,key,rows,color,label,marker='o',ms=5,lw=2,linestyle='-'):
        pairs=[]
        for point in rows:
            xy=coordinates(point,key)
            if xy is not None:pairs.append(xy)
        pairs.sort(key=lambda xy:xy[0])
        if pairs:
            ax.plot([x for x,_ in pairs],[y for _,y in pairs],color=color,marker=marker,ms=ms,
                    lw=lw,linestyle=linestyle,label=label)
    for ax,(key,ylabel,xlabel,color) in zip(axes.flat,panels):
        if reference:
            first={};repeats=[]
            for point in sorted(reference,key=lambda p:p['level']):
                if point['level'] in first:repeats.append(point)
                else:first[point['level']]=point
            series(ax,key,first.values(),'#B8C4CE','历史参考',marker='.',ms=4,lw=1.2)
            series(ax,key,repeats,'#D2A264','历史复测',marker='D',ms=5,lw=0,linestyle='None')
        series(ax,key,points,color,'本轮')
        ax.set_xlabel(xlabel);ax.set_ylabel(ylabel);ax.grid(alpha=.18)
        if key=='thrust_power':ax.set_title('拉力–功率曲线',fontsize=9,pad=3)
        if any(coordinates(p,key) is not None for p in list(points)+list(reference)):
            ax.legend(fontsize=8,loc='best')
    axes.flat[-1].set_visible(False)
    figure.tight_layout(pad=1.7)

class Application:
    def __init__(self,root,smoke=False):
        self.root=root;self.smoke=smoke;self.events=queue.Queue();self.controller=Controller(self.events)
        self.controller.internal_smoke=smoke
        if smoke:
            def forbid_child(*args,**kwargs):
                raise AssertionError('Offline UI smoke test must never start a hardware process')
            self.controller.process_factory=forbid_child
        self.history=[];self.points=[];self.rows={};self._preview_image=None;self._preview_key=None
        self.current_level=None;self.current_index=0;self.total_steps=1;self.live=[];self.last_state=None
        self.motion_started=None;self.last_plot=0;self.last_capture=None;self.stage_started=None
        self.last_heartbeat_write=0
        self.profile_path=CAMERA_CONFIG;self.export_folder=None
        saved=read_json(RUNTIME/'settings.json',{})
        self.mode=tk.StringVar(value='历史回放');self.port=tk.StringVar(value=saved.get('port','COM42'))
        self.specimen=tk.StringVar(value=saved.get('specimen','2006-2150KV / D90S-3'))
        self.template=tk.StringVar(value='低输出验证');self.add_level=tk.StringVar(value='0.060')
        self.cooldown=tk.StringVar(value='5');self.settle=tk.StringVar(value='2')
        self.followup_first=tk.BooleanVar(value=False)
        self.auto_supplement=tk.BooleanVar(value=False)
        self.status=tk.StringVar(value='待机 · 选择方案后开始历史回放')
        self.phase=tk.StringVar(value='当前没有运行中的测试')
        self.data_info=tk.StringVar(value='尚未采集 · 缺测显示为 —')
        self.camera_info=tk.StringVar(value='相机标定：'+read_json(self.profile_path,{}).get('camera','未选择'))
        self.reference=tk.BooleanVar(value=True)
        root.title('电机桨叶测试台 · 上位机 0.1')
        width=min(1380,root.winfo_screenwidth()-60);height=min(880,root.winfo_screenheight()-100)
        root.geometry(f'{width}x{height}');root.minsize(min(1120,width),min(740,height));root.configure(bg=BG)
        root.protocol('WM_DELETE_WINDOW',self.close);root.bind('<Escape>',lambda _:self.stop())
        root.report_callback_exception=self.callback_error
        self._styles();self._build();self.apply_template();self.reload_history()
        self._mode_changed();self._update_resume_button();self.root.after(100,self.tick)

    def _styles(self):
        style=ttk.Style(self.root);style.theme_use('clam')
        style.configure('.',font=('Microsoft YaHei UI',10))
        style.configure('TFrame',background=BG)
        style.configure('TLabel',background=BG,foreground=TEXT)
        style.configure('TButton',padding=(10,7),background='#E2E8F0',foreground=TEXT,borderwidth=0)
        style.map('TButton',background=[('active','#CBD5E1'),('disabled','#E2E8F0')])
        style.configure('Primary.TButton',background=TEAL,foreground='white',font=('Microsoft YaHei UI',11,'bold'))
        style.map('Primary.TButton',background=[('active','#096773'),('disabled','#6C979C')])
        style.configure('Danger.TButton',background=RED,foreground='white',font=('Microsoft YaHei UI',11,'bold'))
        style.map('Danger.TButton',background=[('active','#A52A2A'),('disabled','#C9A5A5')])
        style.configure('Treeview',background='white',fieldbackground='white',foreground=TEXT,rowheight=30,borderwidth=0)
        style.configure('Treeview.Heading',background='#E7EEF4',foreground=TEXT,padding=5,font=('Microsoft YaHei UI',9,'bold'))
        style.map('Treeview',background=[('selected','#D6EEF0')],foreground=[('selected',TEXT)])
        style.configure('TNotebook',background=BG,borderwidth=0)
        style.configure('TNotebook.Tab',padding=(17,10),background='#E1E8EF',foreground=MUTED)
        style.map('TNotebook.Tab',background=[('selected','white')],foreground=[('selected',TEAL)])
        style.configure('Horizontal.TProgressbar',background=TEAL,troughcolor='#E2E8F0',borderwidth=0)

    def _build(self):
        header=tk.Frame(self.root,bg='white',height=66);header.pack(fill='x');header.pack_propagate(False)
        title=tk.Frame(header,bg='white');title.pack(side='left',padx=22,pady=9)
        tk.Label(title,text='电机桨叶测试台',font=('Microsoft YaHei UI',18,'bold'),bg='white',fg=TEXT).pack(anchor='w')
        tk.Label(title,text='自动采集  /  实时监控  /  性能曲线',font=('Microsoft YaHei UI',9),bg='white',fg=MUTED).pack(anchor='w')
        self.mode_badge=tk.Label(header,text='',font=('Microsoft YaHei UI',11,'bold'),padx=14,pady=8)
        self.mode_badge.pack(side='right',padx=20)
        body=tk.Frame(self.root,bg=BG);body.pack(fill='both',expand=True)
        side_outer=tk.Frame(body,bg=NAV,width=292);side_outer.pack(side='left',fill='y');side_outer.pack_propagate(False)
        # Reserve the stop/start controls before the scrollable form can use space.
        bottom=tk.Frame(side_outer,bg=NAV);bottom.pack(side='bottom',fill='x',padx=17,pady=12)
        scroll_holder=tk.Frame(side_outer,bg=NAV);scroll_holder.pack(fill='both',expand=True)
        side_canvas=tk.Canvas(scroll_holder,bg=NAV,highlightthickness=0,width=280)
        side_scroll=ttk.Scrollbar(scroll_holder,orient='vertical',command=side_canvas.yview)
        side_scroll.pack(side='right',fill='y');side_canvas.pack(side='left',fill='both',expand=True)
        side_canvas.configure(yscrollcommand=side_scroll.set)
        side=tk.Frame(side_canvas,bg=NAV)
        side_item=side_canvas.create_window((0,0),window=side,anchor='nw')
        side.bind('<Configure>',lambda _:side_canvas.configure(scrollregion=side_canvas.bbox('all')))
        side_canvas.bind('<Configure>',lambda e:side_canvas.itemconfigure(side_item,width=e.width))
        def label(text):
            tk.Label(side,text=text,bg=NAV,fg='#B5C8D8',anchor='w',font=('Microsoft YaHei UI',9)).pack(fill='x',padx=17,pady=(10,3))
        label('工作模式')
        self.mode_box=ttk.Combobox(side,textvariable=self.mode,values=('历史回放','实机测试'),state='readonly')
        self.mode_box.pack(fill='x',padx=17);self.mode_box.bind('<<ComboboxSelected>>',lambda _:self._mode_changed())
        label('飞控串口')
        row=tk.Frame(side,bg=NAV);row.pack(fill='x',padx=17)
        self.port_box=ttk.Combobox(row,textvariable=self.port,values=('COM42',),width=14)
        self.port_box.pack(side='left',fill='x',expand=True)
        self.scan_button=ttk.Button(row,text='刷新',command=self.refresh_ports);self.scan_button.pack(side='right',padx=(5,0))
        label('电机 / 桨叶标识')
        self.specimen_entry=ttk.Entry(side,textvariable=self.specimen);self.specimen_entry.pack(fill='x',padx=17)
        label('仪表标定')
        tk.Label(side,textvariable=self.camera_info,bg=NAV,fg='white',wraplength=256,justify='left',anchor='w',font=('Microsoft YaHei UI',9)).pack(fill='x',padx=17)
        self.profile_button=ttk.Button(side,text='更换标定配置',command=self.choose_profile);self.profile_button.pack(fill='x',padx=17,pady=(5,0))
        label('测试方案')
        self.template_box=ttk.Combobox(side,textvariable=self.template,values=tuple(TEMPLATES),state='readonly')
        self.template_box.pack(fill='x',padx=17);self.template_box.bind('<<ComboboxSelected>>',lambda _:self.apply_template())
        self.plan_tree=ttk.Treeview(side,columns=('level','hold','stage'),show='headings',height=5,selectmode='extended')
        for key,title,width in [('level','指令',86),('hold','保持',54),('stage','档段',85)]:
            self.plan_tree.heading(key,text=title);self.plan_tree.column(key,width=width,anchor='center',stretch=True)
        self.plan_tree.pack(fill='x',padx=17,pady=(7,5))
        row=tk.Frame(side,bg=NAV);row.pack(fill='x',padx=17)
        self.level_box=ttk.Combobox(row,textvariable=self.add_level,values=[f'{v:.3f}' for v in LEVELS],width=8,state='normal');self.level_box.pack(side='left')
        self.add_button=ttk.Button(row,text='加入',command=self.add_step);self.add_button.pack(side='left',padx=4)
        self.remove_button=ttk.Button(row,text='移除',command=self.remove_step);self.remove_button.pack(side='right')
        tk.Label(side,text=f'可直接输入 {MIN_LEVEL:.3f}～{MAX_LEVEL:.3f}，最多三位小数',bg=NAV,fg='#8FA9BD',
                 anchor='w',font=('Microsoft YaHei UI',8)).pack(fill='x',padx=17,pady=(0,2))
        self.supplement_button=ttk.Button(side,text='按300–500g补8点',command=self.supplement)
        self.supplement_button.pack(fill='x',padx=17,pady=(6,0))
        self.auto_supplement_check=tk.Checkbutton(side,text='主测后自动补测300–500g（8点）',
            variable=self.auto_supplement,command=self.update_plan_note,bg=NAV,fg='#CBD5E1',selectcolor=NAV,
            activebackground=NAV,activeforeground='white',anchor='w',font=('Microsoft YaHei UI',9))
        self.auto_supplement_check.pack(fill='x',padx=13)
        self.followup_check=tk.Checkbutton(side,text='首档接续前一轮（忽略残余拉力）',
            variable=self.followup_first,bg=NAV,fg='#CBD5E1',selectcolor=NAV,
            activebackground=NAV,activeforeground='white',anchor='w',
            font=('Microsoft YaHei UI',9))
        self.followup_check.pack(fill='x',padx=13)
        row=tk.Frame(side,bg=NAV);row.pack(fill='x',padx=17,pady=(9,3))
        tk.Label(row,text='档间停机 / 秒',bg=NAV,fg='#CBD5E1',font=('Microsoft YaHei UI',9)).pack(side='left')
        self.cooldown_box=ttk.Spinbox(row,textvariable=self.cooldown,from_=5,to=120,width=5);self.cooldown_box.pack(side='right')
        row=tk.Frame(side,bg=NAV);row.pack(fill='x',padx=17,pady=3)
        tk.Label(row,text='忽略目标档前 / 秒',bg=NAV,fg='#CBD5E1',font=('Microsoft YaHei UI',9)).pack(side='left')
        self.settle_box=ttk.Spinbox(row,textvariable=self.settle,from_=0,to=5,width=5);self.settle_box.pack(side='right')
        self.plan_note=tk.Label(side,text='',bg=NAV,fg='#AFC4D4',wraplength=254,justify='left',anchor='w',font=('Microsoft YaHei UI',9))
        self.plan_note.pack(fill='x',padx=17,pady=(7,3))
        self.prepare_button=ttk.Button(bottom,text='设备预检 · 不转电机',command=self.prepare);self.prepare_button.pack(fill='x',pady=4)
        self.start_button=ttk.Button(bottom,text='开始历史回放',style='Primary.TButton',command=self.start);self.start_button.pack(fill='x',pady=4)
        self.stop_button=ttk.Button(bottom,text='停止测试   Esc',style='Danger.TButton',command=self.stop,state='disabled');self.stop_button.pack(fill='x',pady=4)
        main=ttk.Frame(body,padding=(17,12));main.pack(side='left',fill='both',expand=True)
        footer=ttk.Frame(main);footer.pack(side='bottom',fill='x',pady=(9,0))
        status=ttk.Frame(main);status.pack(fill='x')
        ttk.Label(status,textvariable=self.status,font=('Microsoft YaHei UI',13,'bold')).pack(side='left')
        self.confirm_button=ttk.Button(status,text='确认现场已停转',command=self.confirm_stop,state='disabled');self.confirm_button.pack(side='right')
        self.resume_button=ttk.Button(status,text='从失败档继续',command=self.resume,state='disabled');self.resume_button.pack(side='right',padx=(0,8))
        ttk.Label(main,textvariable=self.phase,foreground=MUTED,wraplength=760,justify='left').pack(anchor='w',pady=(5,7))
        self.progress=ttk.Progressbar(main,maximum=100);self.progress.pack(fill='x',pady=(0,10))
        cards=ttk.Frame(main);cards.pack(fill='x',pady=(0,10));self.metric_vars={}
        for index,(key,name,unit) in enumerate([('upper','拉力','g'),('voltage','电压','V'),('current','电流','A'),('power','输入功率 · 计算值','W')]):
            card=tk.Frame(cards,bg='white',highlightthickness=1,highlightbackground='#E1E8EF',padx=15,pady=9)
            card.grid(row=0,column=index,sticky='nsew',padx=(0,8 if index<3 else 0));cards.columnconfigure(index,weight=1)
            tk.Label(card,text=name,bg='white',fg=MUTED,font=('Microsoft YaHei UI',9)).pack(anchor='w')
            value=tk.StringVar(value='—');self.metric_vars[key]=value
            row=tk.Frame(card,bg='white');row.pack(fill='x')
            tk.Label(row,textvariable=value,bg='white',fg=TEXT,font=('Segoe UI',25,'bold')).pack(side='left')
            tk.Label(row,text=unit,bg='white',fg=MUTED,font=('Segoe UI',11)).pack(side='left',padx=5,anchor='s',pady=6)
        self.notebook=ttk.Notebook(main);self.notebook.pack(fill='both',expand=True)
        self.monitor_tab=ttk.Frame(self.notebook,padding=10);self.curves_tab=ttk.Frame(self.notebook,padding=8);self.history_tab=ttk.Frame(self.notebook,padding=10)
        for frame,name in [(self.monitor_tab,'实时监控'),(self.curves_tab,'测试曲线'),(self.history_tab,'历史记录')]:self.notebook.add(frame,text=name)
        self._monitor();self._curves();self._history()
        ttk.Label(footer,textvariable=self.data_info,foreground=MUTED,font=('Microsoft YaHei UI',9)).pack(side='left')
        ttk.Button(footer,text='打开本轮记录',command=self.open_records).pack(side='right')
        ttk.Button(footer,text='导出本轮',command=self.export).pack(side='right',padx=6)

    def _monitor(self):
        self.monitor_tab.columnconfigure(0,weight=2);self.monitor_tab.columnconfigure(1,weight=3);self.monitor_tab.rowconfigure(0,weight=1)
        camera=ttk.Frame(self.monitor_tab);camera.grid(row=0,column=0,sticky='nsew',padx=(0,10))
        ttk.Label(camera,text='仪表画面',font=('Microsoft YaHei UI',11,'bold')).pack(anchor='w',pady=(2,8))
        self.preview=tk.Label(camera,text='历史回放可显示已保存的仪表照片\n实机预检后显示相机画面',bg='#182D3D',fg='#CAD8E4',font=('Microsoft YaHei UI',10),height=12)
        self.preview.pack(fill='both',expand=True)
        self.quality=tk.StringVar(value='数据状态：等待采集')
        ttk.Label(camera,textvariable=self.quality,foreground=MUTED,wraplength=360,justify='left').pack(fill='x',pady=10)
        ttk.Label(camera,text='输入功率统一由同帧电压×电流计算。\n原始仪表读数和拒读原因随会话保存。',foreground=MUTED,wraplength=360,font=('Microsoft YaHei UI',9)).pack(anchor='w')
        plot=ttk.Frame(self.monitor_tab);plot.grid(row=0,column=1,sticky='nsew')
        self.live_figure=Figure(figsize=(5,4.8),dpi=90,facecolor='white');self.live_canvas=FigureCanvasTkAgg(self.live_figure,plot)
        self.live_canvas.get_tk_widget().pack(fill='both',expand=True);self.draw_live()

    def _curves(self):
        top=ttk.Frame(self.curves_tab);top.pack(fill='x')
        ttk.Checkbutton(top,text='显示历史参考（统计口径存在差异）',variable=self.reference,command=self.draw_curves).pack(side='left')
        ttk.Label(top,text='重复测点单独保留',foreground=MUTED).pack(side='right')
        self.curve_figure=Figure(figsize=(9.6,5.6),dpi=90,facecolor='white');self.curve_canvas=FigureCanvasTkAgg(self.curve_figure,self.curves_tab)
        self.curve_canvas.get_tk_widget().pack(fill='both',expand=True)

    def _history(self):
        top=ttk.Frame(self.history_tab);top.pack(fill='x',pady=(0,8))
        ttk.Button(top,text='刷新记录',command=self.reload_history).pack(side='left')
        ttk.Button(top,text='载入汇总CSV',command=self.import_csv).pack(side='left',padx=7)
        ttk.Button(top,text='打开选中原始记录',command=self.open_selected).pack(side='right')
        cols=('level','thrust','current','power','eff','source')
        self.history_tree=ttk.Treeview(self.history_tab,columns=cols,show='headings',height=10)
        for key,title,width in zip(cols,['指令','拉力 / g','电流 / A','功率 / W','效率 / g/W','数据来源'],[80,110,100,105,110,200]):
            self.history_tree.heading(key,text=title);self.history_tree.column(key,width=width,anchor='center',stretch=True)
        self.history_tree.pack(fill='both',expand=True)
        self.history_tree.bind('<<TreeviewSelect>>',lambda _:self.history_detail())
        self.history_note=tk.StringVar(value='选择记录查看统计范围与原始数据位置。')
        ttk.Label(self.history_tab,textvariable=self.history_note,wraplength=850,foreground=MUTED,justify='left').pack(fill='x',pady=8)

    def plan(self):
        levels=tuple(sorted(float(self.plan_tree.set(i,'level').split()[0]) for i in self.plan_tree.get_children()))
        return Plan(levels,float(self.cooldown.get()),float(self.settle.get()),
                    self.followup_first.get(),self.auto_supplement.get())

    def apply_template(self):
        if self.controller.active:return
        self.followup_first.set(False)
        self.auto_supplement.set(self.template.get()==MAIN_TEMPLATE)
        for item in self.plan_tree.get_children():self.plan_tree.delete(item)
        for level in TEMPLATES[self.template.get()]:self._insert_step(level)
        self.update_plan_note()

    def _insert_step(self,level):
        step=Step(level)
        self.plan_tree.insert('', 'end',iid=f'{level:g}',values=(f'{level:.3f}  {level*100:g}%',f'{step.hold_seconds}s',step.stage))

    def add_step(self):
        if self.controller.active:return
        text=self.add_level.get().strip()
        try:level=float(text)
        except ValueError:
            messagebox.showwarning('无法加入档位','请输入数字油门指令，例如0.073。',parent=self.root);return
        try:Step(level)
        except ValueError as exc:
            messagebox.showwarning('无法加入档位',str(exc),parent=self.root);return
        self.add_level.set(f'{level:.3f}')
        if not self.plan_tree.exists(f'{level:g}'):
            self._insert_step(level);self.auto_supplement.set(False)
        for index,item in enumerate(sorted(self.plan_tree.get_children(),key=float)):self.plan_tree.move(item,'',index)
        self.update_plan_note()

    def remove_step(self):
        if self.controller.active:return
        if self.plan_tree.selection():self.auto_supplement.set(False)
        for item in self.plan_tree.selection():self.plan_tree.delete(item)
        self.update_plan_note()

    def _latest_measured_session(self):
        """Newest real session with points, for supplementing after a restart."""
        candidates=[]
        for path in DATA.glob('*/session.json'):
            session=read_json(path)
            if not session or session.get('mode')!='实机测试' or not session.get('points'):
                continue
            try:candidates.append((path.stat().st_mtime,path.parent,session))
            except OSError:continue
        if not candidates:return None
        _,folder,session=max(candidates,key=lambda item:item[0])
        return folder,session

    def supplement(self):
        """Build a separate 300..500 g refinement plan from measured thrust."""
        if self.controller.active:return
        points=[];source=None;session=self.controller.session
        if session and session.get('mode')=='实机测试' and self.points and self.controller.folder:
            points=self.points;source=f'本轮会话 {self.controller.folder.name}'
        else:
            latest=self._latest_measured_session()
            if latest:
                folder,stored=latest
                points=stored.get('points') or []
                source=f'最近实机会话 {folder.name}'
        if not points:
            messagebox.showwarning('没有已测数据','请先完成覆盖300–500g的实机测量，再生成独立补点方案。',parent=self.root)
            return
        try:levels=supplement_levels(points)
        except ValueError as exc:
            messagebox.showwarning('无法生成补点',str(exc),parent=self.root);return
        listed='、'.join(f'{level:.3f}' for level in levels)
        if not messagebox.askyesno('生成300–500g补点方案',
                f'数据来源：{source}\n\n'
                f'在已测拉力覆盖区间内，按均匀拉力目标插值生成 {len(levels)} 个油门档位：\n{listed}\n\n'
                f'这些档位将替换当前方案（不会删除已保存数据）。每个档位保持10秒，档间停机 {self.cooldown.get()} 秒。\n\n继续吗？',
                parent=self.root):
            return
        for item in self.plan_tree.get_children():self.plan_tree.delete(item)
        for level in levels:self._insert_step(level)
        self.auto_supplement.set(False)
        self.followup_first.set(True)
        self.update_plan_note()
        self.plan_note.configure(text=f"300–500g 补点方案 · {len(levels)}档\n来源：{source}\n"
            f"每档保持10秒；档间停机{self.cooldown.get()}秒。\n"
            f"首档按接续前一轮处理，允许任意残余拉力；启动前仍需三项现场确认。")

    def update_plan_note(self):
        try:
            p=self.plan();max_level=max(p.levels)
            auto='\n随后自动补测300–500g共8点，整轮18点。' if p.auto_supplement else ''
            self.plan_note.configure(text=f"{len(p.levels)}个主档位 · 最高{max_level*100:g}%{auto}\n不设固定电流/功率/拉力中止门槛\n过载保护由电调、电源和现场独立断电负责。")
        except ValueError:self.plan_note.configure(text='请至少加入一个档位。')

    def _mode_changed(self):
        real=self.mode.get()=='实机测试'
        self.mode_badge.configure(text='实机测试 · 启动前需现场确认' if real else '历史回放 · 不连接设备',bg='#FDE7E7' if real else '#FFF0D4',fg='#9E3030' if real else '#986014')
        self.start_button.configure(text='开始实机测试' if real else '开始历史回放')
        self.prepare_button.configure(state='normal' if real and not self.controller.active else 'disabled')
        if not self.controller.active and not self.controller.session:
            self.status.set('待机 · 请先预检并确认实机方案' if real else '待机 · 选择方案后开始历史回放')

    def choose_profile(self):
        path=filedialog.askopenfilename(title='选择仪表标定配置',initialdir=ROOT/'cv_mvp',filetypes=[('标定配置','*.json')])
        if not path:return
        from .model import validate_profile
        try:config=validate_profile(path)
        except Exception as exc:messagebox.showerror('无法载入标定',str(exc));return
        self.profile_path=Path(path);self.camera_info.set('相机标定：'+config['camera'])

    def refresh_ports(self):
        if self.controller.active:return
        self.scan_button.configure(state='disabled')
        def work():
            try:self.events.put(dict(kind='ports',ports=scan_ports()))
            except Exception as exc:self.events.put(dict(kind='ports',error=str(exc)))
        threading.Thread(target=work,daemon=True).start()

    def _operator_dialog(self,plan,resume=False):
        dialog=tk.Toplevel(self.root);dialog.title('确认续测方案' if resume else '确认本轮实机方案')
        dialog.transient(self.root);dialog.grab_set()
        dialog.geometry(f'{min(760,self.root.winfo_screenwidth()-60)}x{min(740,self.root.winfo_screenheight()-90)}')
        dialog.bind('<Escape>',lambda _:dialog.destroy())
        outer=ttk.Frame(dialog,padding=10);outer.pack(fill='both',expand=True)
        buttons=ttk.Frame(outer);buttons.pack(side='bottom',fill='x',pady=(10,0))
        canvas=tk.Canvas(outer,highlightthickness=0)
        scroll=ttk.Scrollbar(outer,orient='vertical',command=canvas.yview)
        scroll.pack(side='right',fill='y');canvas.pack(side='left',fill='both',expand=True)
        canvas.configure(yscrollcommand=scroll.set)
        frame=ttk.Frame(canvas,padding=10)
        item=canvas.create_window((0,0),window=frame,anchor='nw')
        frame.bind('<Configure>',lambda _:canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>',lambda event:canvas.itemconfigure(item,width=event.width))
        ttk.Label(frame,text=('续测从失败档开始；已完成档位保留，不会重跑。' if resume
                             else '确认后将按下表自动执行，异常时停止整个方案。'),
                  font=('Microsoft YaHei UI',12,'bold')).pack(anchor='w')
        ttk.Label(frame,text=f"串口 {self.port.get()} · Motor 1 · 档间停机 {plan.cooldown_seconds:g}秒 · 统计忽略前{plan.settle_seconds:g}秒",foreground=MUTED).pack(anchor='w',pady=8)
        if plan.followup_first:
            ttk.Label(frame,text='首档按接续前一轮处理：启动前设置基线允许任意残余拉力；电压、电流、功率仍必须归零。',
                      foreground=RED,wraplength=620,justify='left').pack(anchor='w')
        if resume:
            ttk.Label(frame,text='相机标定沿用原会话快照。若相机位置、镜头、仪表或曝光已改变，请取消并重新标定后开始新会话。',
                      foreground=RED,wraplength=620,justify='left').pack(anchor='w')
        if plan.auto_supplement:
            policy=(self.controller.session or {}).get('supplement',{}) if resume else {}
            text=('将执行已保存的剩余补点，完成后结束本轮。' if policy.get('levels') else
                  '主测全部成功后自动生成并执行300–500g内8个补点（不含两端），无需再次点击开始。'
                  '补点仅用本轮主测插值，输出不超出已测范围；覆盖不足、非单调或异常时停止。')
            ttk.Label(frame,text=text,foreground=TEAL,wraplength=675,justify='left').pack(anchor='w',pady=5)
        if 1.0 in plan.levels:
            ttk.Label(frame,text='本方案包含100%输出（1.000），此前尚未完成该档实机验收；请核对当前设备允许范围。',
                      foreground=RED,wraplength=675,justify='left').pack(anchor='w',pady=3)
        tree=ttk.Treeview(frame,columns=('level','time','stage'),show='headings',height=8)
        for key,title,width in [('level','输出指令',120),('time','缓升+保持',150),('stage','档段',100)]:
            tree.heading(key,text=title);tree.column(key,width=width,anchor='center')
        for v in plan.levels:
            s=Step(v)
            tree.insert('','end',values=(f'{v:.3f}',f'{s.rise_seconds:g}+{s.hold_seconds:g}秒',s.stage))
        tree.pack(fill='both',expand=True)
        ttk.Label(frame,text=('续测会保留本轮已完成的点和原始目录，只执行下表未完成档位；现场确认必须重新完成。\n' if resume else '')
                  +'本轮不设固定电流/功率/拉力中止门槛，数据只记录不断言设备极限。\n电机、电调、电源和夹具保护由现场独立切断负责；软件仍会在停止失败、输出异常、相机或界面心跳失效时中止。\n若某档仅因有效读数不足10个而失败，软件会在停机后单独重测该档一次，再继续后续档位。',foreground=MUTED,wraplength=620,justify='left').pack(anchor='w',pady=9)
        labels=['台架与桨叶固定牢靠，旋转区域已隔离，无需手扶',
                '现场有人看护，可立即独立切断电调动力电源',
                '已核对本轮设备与档位，并确认不设固定中止门槛，接受按此方案自动运行']
        checks=[tk.BooleanVar(value=False) for _ in labels]
        for var,label in zip(checks,labels):ttk.Checkbutton(frame,text=label,variable=var).pack(anchor='w',pady=3)
        outcome={}
        def accept():
            if not all(v.get() for v in checks):messagebox.showwarning('还未确认','请完成三项现场确认。',parent=dialog);return
            outcome.update(confirmed=True,plan_signature=plan.signature,
                confirmed_at=datetime.now().astimezone().isoformat(),checks=dict(zip(labels,[True]*3)))
            dialog.destroy()
        ttk.Button(buttons,text='取消',command=dialog.destroy).pack(side='right')
        ttk.Button(buttons,text=('确认并继续续测' if resume else '确认并开始本轮实机测试'),
                   style='Primary.TButton',command=accept).pack(side='right',padx=10)
        self.root.wait_window(dialog)
        return outcome or None

    def start(self):
        if self.controller.active:return
        try:
            plan=self.plan();real=self.mode.get()=='实机测试'
            operator=self._operator_dialog(plan) if real else None
            if real and not operator:return
            self._reset_run()
            self.controller.start(plan,'real' if real else 'demo',self.port.get().strip(),self.profile_path,self.specimen.get(),operator)
            self._set_busy(True)
        except Exception as exc:messagebox.showerror('无法开始',str(exc))

    def prepare(self):
        if self.controller.active or self.mode.get()!='实机测试':return
        try:
            self._reset_run();self.controller.start(self.plan(),'real',self.port.get().strip(),self.profile_path,self.specimen.get(),prepare_only=True)
            self._set_busy(True)
        except Exception as exc:messagebox.showerror('无法预检',str(exc))

    def _reset_run(self):
        self.points=[];self.live=[];self.last_state=None;self.last_capture=None;self.motion_started=None
        self.current_index=0;self.progress['value']=0;self._preview_key=None
        self.confirm_button.configure(state='disabled');self.notebook.select(self.monitor_tab)
        for v in self.metric_vars.values():v.set('—')
        self.draw_curves();self.draw_live()

    def _set_busy(self,busy):
        for widget in (self.mode_box,self.port_box,self.profile_button,self.template_box,self.level_box,self.add_button,self.remove_button,self.supplement_button,self.auto_supplement_check,self.followup_check,self.cooldown_box,self.settle_box,self.specimen_entry,self.start_button,self.scan_button):
            widget.configure(state='disabled' if busy else 'normal')
        if not busy:
            for widget in (self.mode_box,self.template_box):widget.configure(state='readonly')
            self.level_box.configure(state='normal')
        self.prepare_button.configure(state='normal' if not busy and self.mode.get()=='实机测试' else 'disabled')
        pending=self.controller.session and self.controller.session['status'].endswith('waiting_operator')
        if pending:
            self.start_button.configure(state='disabled');self.prepare_button.configure(state='disabled');self.mode_box.configure(state='disabled')
        self.resume_button.configure(state='disabled' if busy else ('normal' if self._can_resume() else 'disabled'))
        self.stop_button.configure(state='normal' if busy else 'disabled')

    def stop(self):
        if self.controller.active:self.controller.stop();self.status.set('正在停止 · 不会接续下一档')

    def confirm_stop(self):
        if messagebox.askyesno('现场停转确认','请确认：电机已完全停止、夹具牢固，且无明显振动、异响或异常发热。\n\n以上全部符合吗？',parent=self.root):
            try:self.controller.confirm_physical_stop()
            except ValueError as exc:messagebox.showerror('无法确认',str(exc))

    def _can_resume(self):
        if self.controller.active:return False
        if self.controller.can_resume():return True
        return self.controller.latest_resumable() is not None

    def _update_resume_button(self):
        if not hasattr(self,'resume_button'):return
        self.resume_button.configure(state='normal' if self._can_resume() else 'disabled')

    def resume(self):
        if self.controller.active:return
        try:
            if not self.controller.can_resume():
                latest=self.controller.latest_resumable()
                if not latest:raise ValueError('没有可续测的已停机实机会话。')
                _,folder,session=latest
                if not messagebox.askyesno('载入中止会话',
                        f'载入最近的中止会话继续测试？\n\n{folder}\n\n该会话已确认现场停转；续测从失败档开始，已完成档位不会重跑。',
                        parent=self.root):return
                self.controller.load_latest_resumable()
            session=self.controller.session
            if not self.controller.can_resume(session):
                raise ValueError('当前没有可续测的已停机会话。')
            self.export_folder=self.controller.folder
            self.points=list(session.get('points') or [])
            if session.get('specimen') is not None:self.specimen.set(session['specimen'])
            if session.get('port'):self.port.set(session['port'])
            self.status.set('已载入中止会话 · 现场已确认停转')
            self.phase.set('点击确认后从失败档开始续测；已完成档位不会重跑。')
            self.reload_table();self.draw_curves()
            info=self.controller.resume_remaining_levels(session)
            if not info:raise ValueError('原方案没有未完成的档位。')
            _,levels=info
            original=session['plan']
            plan=Plan(levels,original['cooldown_seconds'],original['settle_seconds'],
                      auto_supplement=original.get('auto_supplement',False))
            if session.get('port'):self.port.set(session['port'])
            if session.get('specimen') is not None:self.specimen.set(session['specimen'])
            self.export_folder=self.controller.folder
            operator=self._operator_dialog(plan,resume=True)
            if not operator:return
            self.controller.resume(plan,operator,self.port.get().strip())
            self.points=[p for p in (self.controller.session.get('points') or [])]
            self.live=[];self.motion_started=None;self.last_capture=None;self.stage_started=None
            self.last_state=None;self._preview_key=None
            for value in self.metric_vars.values():value.set('—')
            self.current_index=0;self.total_steps=len(levels);self.progress['value']=0
            self.confirm_button.configure(state='disabled')
            self.notebook.select(self.monitor_tab);self.draw_curves();self.reload_table()
            self._set_busy(True)
            self.status.set('续测准备中 · 从失败档开始')
            self.phase.set('续测已确认；已完成档位保留，不会重跑。')
        except Exception as exc:messagebox.showerror('无法继续',str(exc))

    def tick(self):
        if time.monotonic()-self.last_heartbeat_write>=.25:
            self.controller.touch_heartbeat();self.last_heartbeat_write=time.monotonic()
        for _ in range(160):
            try:event=self.events.get_nowait()
            except queue.Empty:break
            self._event(event)
        if self.last_state and not self.last_state.get('demo'):
            age=time.monotonic()-self.last_state.get('captured_monotonic_ns',0)/1e9
            if not 0<=age<=1 or self.last_state.get('state')!='running':
                for v in self.metric_vars.values():v.set('—')
                if self.controller.active:self.data_info.set('画面未更新或采集已结束 · 不沿用旧读数')
        if self.motion_started and self.controller.active and self.current_level:
            elapsed=time.monotonic()-self.motion_started;s=Step(self.current_level)
            self.progress['value']=100*(self.current_index+min(elapsed/(s.rise_seconds+s.hold_seconds),.99))/self.total_steps
        if self.live and time.monotonic()-self.last_plot>.5:
            self.draw_live();self.last_plot=time.monotonic()
        self.root.after(100,self.tick)

    def _event(self,event):
        kind=event['kind']
        if kind=='ports':
            self.scan_button.configure(state='normal')
            if event.get('error'):self.phase.set('串口扫描失败：'+event['error']);return
            ports=[p['port'] for p in event['ports']];self.port_box.configure(values=ports)
            if self.port.get() not in ports and ports:self.port.set(ports[0])
            self.phase.set('可用串口：'+('、'.join(ports) or '未找到'))
        elif kind=='session':
            self.export_folder=Path(event['folder']);self.status.set('正在准备会话')
        elif kind=='step':
            self.current_level=event['level'];self.current_index=event['index'];self.total_steps=event['total']
            self.live=[];self.motion_started=None;self.last_capture=None;self.stage_started=None
            prefix=('自动补点 · ' if event.get('phase')=='supplement' else '主测 · ')
            if event.get('resume_number'):prefix='续测 · '+prefix
            self.status.set(prefix+f"第{event['index']+1}/{event['total']}档 · 指令{event['level']:.3f}（{event['level']*100:g}%）")
        elif kind=='phase':self.phase.set(event['text'])
        elif kind=='schedule':
            self.total_steps=event['total'];self.phase.set(event['text'])
            self.plan_note.configure(text=event['text']+'\n补点计划已保存，停机等待后自动执行。')
        elif kind=='log':
            if event['text'].startswith('STEP_START:'):self.motion_started=time.monotonic()
        elif kind=='live':self.show_live(event.get('state'),event.get('preview'))
        elif kind=='point':
            self.points.append(event['point']);self.draw_curves();self.reload_table()
            self.progress['value']=100*(self.current_index+1)/self.total_steps;self.motion_started=None
            warnings=(event['point'].get('quality') or {}).get('warnings') or []
            if warnings:self.phase.set('统计提示：'+'；'.join(warnings))
        elif kind in ('error','alarm'):
            self.phase.set(event['text']);self.status.set('测试已中止，请检查记录与现场状态')
            if kind=='alarm' and not self.smoke:messagebox.showerror('请立即检查现场',event['text'])
        elif kind=='finished':
            self._set_busy(False);session=event['session'];self.status.set(STATUS.get(session['status'],session['status']))
            self.motion_started=None;self.export_folder=Path(event['folder'])
            self.confirm_button.configure(state='normal' if session['status'].endswith('waiting_operator') else 'disabled')
            self.phase.set(session.get('error') or '记录已自动保存；可查看曲线或导出本轮结果。')
            if session['status']=='demo_complete':self.progress['value']=100
            self.draw_curves();self.reload_table();self._update_resume_button()
            if self.points:self.curve_figure.savefig(self.export_folder/'curves.png',dpi=150)

    def show_live(self,state,preview=None):
        self.last_state=state
        if not state:
            for v in self.metric_vars.values():v.set('—')
            return
        demo=state.get('demo',False)
        age=time.monotonic()-state.get('captured_monotonic_ns',0)/1e9
        fresh=demo or state.get('state')=='running' and 0<=age<=1
        fields={f['id']:f for f in state.get('fields',[])}
        values={k:number(fields.get(k,{}),u) if fresh else None for k,u in [('upper','g'),('voltage','V'),('current','A')]}
        values['power']=values['voltage']*values['current'] if values['voltage'] is not None and values['current'] is not None else None
        for key,var in self.metric_vars.items():var.set(text_number(values[key],0 if key=='upper' else 2))
        valid=sum(f.get('valid') is True for f in fields.values());reasons=[f.get('reason') for f in fields.values() if not f.get('valid') and f.get('reason')]
        self.quality.set(f"本帧有效 {valid}/{len(fields)} 项\n整轮拉力可读率 {state.get('readable_fraction',{}).get('upper',0):.0%}\n"+('；'.join(reasons[:2]) or '当前数字与单位检查通过'))
        self.data_info.set('历史记录回放 · 不代表当前设备读数' if demo else f"画面年龄 {age:.2f}秒 · 相机 {state.get('capture_fps',0):.1f}帧/秒")
        timestamp=state.get('demo_elapsed_seconds') if demo else state.get('captured_monotonic_ns',0)/1e9
        if self.last_capture!=timestamp:
            if self.stage_started is None:self.stage_started=0 if demo else timestamp
            self.live.append((timestamp-self.stage_started,values['upper'],values['current'],values['power']));self.live=self.live[-600:];self.last_capture=timestamp
        if demo:self.progress['value']=100*(self.current_index+min(timestamp/Step(self.current_level).hold_seconds,.99))/self.total_steps
        if preview and Path(preview).exists():
            try:
                stamp=(preview,Path(preview).stat().st_mtime_ns)
                if stamp!=self._preview_key:
                    with Image.open(preview) as image:im=image.convert('RGB').copy()
                    width=max(260,self.preview.winfo_width());height=max(180,self.preview.winfo_height())
                    im=ImageOps.contain(im,(width,height));self._preview_image=ImageTk.PhotoImage(im)
                    self.preview.configure(image=self._preview_image,text='',height=0);self._preview_key=stamp
            except OSError:pass

    def draw_live(self):
        self.live_figure.clear();axes=self.live_figure.subplots(3,1,sharex=True)
        for index,(ax,label,color) in enumerate(zip(axes,['拉力 / g','电流 / A','功率 / W'],[TEAL,'#377DCC','#865ABC']),1):
            if self.live:ax.plot([r[0] for r in self.live],[float('nan') if r[index] is None else r[index] for r in self.live],color=color,lw=1.5)
            ax.set_ylabel(label);ax.grid(alpha=.18)
        axes[-1].set_xlabel('本档采集时间 / 秒');self.live_figure.tight_layout(pad=1.2);self.live_canvas.draw_idle()

    def draw_curves(self):
        draw_curves(self.curve_figure,self.points,self.history if self.reference.get() else [])
        self.curve_canvas.draw_idle()

    def reload_history(self):
        try:self.history=load_campaign()
        except (OSError,ValueError):self.history=[]
        self.reload_table();self.draw_curves()

    def reload_table(self):
        for item in self.history_tree.get_children():self.history_tree.delete(item)
        self.rows={}
        sources=[*self.history]
        current_dirs={p.get('run_dir') for p in self.points}
        for session_path in sorted(DATA.glob('*/session.json'),reverse=True) if DATA.exists() else []:
            session=read_json(session_path,{})
            if session.get('internal_smoke'):continue
            if self.controller.folder and session_path.parent==self.controller.folder:continue
            sources.extend(p for p in session.get('points',[]) if p.get('run_dir') not in current_dirs)
        sources.extend(self.points)
        for index,point in enumerate(sources):
            m=point['metrics'];source={'historical_campaign':'历史汇总','historical_replay':'历史回放','measurement':'实机采集'}.get(point.get('source_kind'),'记录')
            if point.get('phase')=='supplement':source+=' · 自动补点'
            values=(f"{point['level']:.3f}",text_number(m['thrust_g'].get('mean')),text_number(m['current_a'].get('mean'),3),text_number(m['power_w'].get('mean')),text_number(point.get('efficiency_g_per_w')),source)
            key=str(index);self.history_tree.insert('','end',iid=key,values=values);self.rows[key]=point

    def history_detail(self):
        selection=self.history_tree.selection()
        if not selection:return
        point=self.rows[selection[0]]
        self.history_note.set(point.get('note') or f"目标保持 {point.get('hold_seconds','—')}秒，忽略前{point.get('settle_seconds','—')}秒；窗口样本{point.get('samples','—')}。原始记录：{point['run_dir']}")

    def import_csv(self):
        path=filedialog.askopenfilename(title='载入历史汇总',filetypes=[('CSV汇总','*.csv')])
        if path:
            try:self.history=load_campaign(path);self.reload_table();self.draw_curves()
            except Exception as exc:messagebox.showerror('无法载入',str(exc))

    def open_selected(self):
        selection=self.history_tree.selection()
        if selection:self._open_path(self.rows[selection[0]]['run_dir'])

    def _open_path(self,path):
        try:os.startfile(str(Path(path).resolve()))
        except OSError as exc:messagebox.showerror('无法打开记录',str(exc))

    def open_records(self):
        self._open_path(self.export_folder or ROOT/'integration_runs/campaign_summary_20261018')

    def export(self):
        if self.controller.active:messagebox.showinfo('正在测试','请等待本轮保存结束后导出。');return
        if not self.controller.session:messagebox.showinfo('还没有本轮记录','先运行历史回放或实机测试。历史数据可在“历史记录”中打开。');return
        folder=filedialog.askdirectory(title='选择导出文件夹')
        if not folder:return
        # A unique subfolder avoids overwriting previous exports.
        out=Path(folder)/self.controller.session['id']
        if out.exists():out=out/('export_'+datetime.now().strftime('%H%M%S'))
        export_session(self.controller.session,out);self.curve_figure.savefig(out/'curves.png',dpi=170)
        messagebox.showinfo('导出完成','已保存数据表、曲线图、会话记录和测试报告。')

    def close(self):
        if self.controller.active:
            self.stop();messagebox.showinfo('正在停止','已请求停止。请等待执行进程收尾，核对现场停转后再关闭。');return
        if self.controller.session and self.controller.session['status'].endswith('waiting_operator'):
            messagebox.showwarning('需要现场确认','请先确认现场完全停转。软件输出回零不能代替现场确认。');return
        write_json(RUNTIME/'settings.json',dict(port=self.port.get(),specimen=self.specimen.get()))
        self.root.destroy()

    def callback_error(self,exc_type,exc,tb):
        import traceback
        if self.controller.active:self.controller.stop()
        (RUNTIME/'ui_error.log').write_text(''.join(traceback.format_exception(exc_type,exc,tb)),encoding='utf-8')
        messagebox.showerror('界面异常','已请求停止当前任务。错误记录已保存：'+str(exc))

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--smoke-test',action='store_true');args=parser.parse_args()
    root=tk.Tk()
    if args.smoke_test:root.withdraw()
    app=Application(root,args.smoke_test)
    if args.smoke_test:
        root.update();assert len(app.history)==28;assert app.mode.get()=='历史回放';assert not app.controller.active
        assert app.plan().levels==TEMPLATES['低输出验证']
        app.start();deadline=time.monotonic()+30
        while app.status.get()!=STATUS['demo_complete'] and time.monotonic()<deadline:
            root.update();time.sleep(.02)
        root.update();assert not app.controller.active,'Demo timed out';assert len(app.points)==5
        assert app.controller.session['status']=='demo_complete'
        result=dict(history_points=len(app.history),demo_points=len(app.points),mode=app.controller.session['mode'],
                    status=app.controller.session['status'],session_folder=str(app.controller.folder),hardware_commands_sent=False)
        write_json(RUNTIME/'smoke_report.json',result);print(json.dumps(result,ensure_ascii=False));root.destroy()
    else:root.mainloop()

if __name__=='__main__':main()
