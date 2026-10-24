from pathlib import Path
import sys
import traceback

try:
    from .app import main
    main()
except Exception as exc:
    error=traceback.format_exc()
    folder=Path(__file__).resolve().parent/'runtime'
    try:
        folder.mkdir(exist_ok=True)
        (folder/'startup_error.log').write_text(error,encoding='utf-8')
    except OSError:
        pass
    if '--smoke-test' not in sys.argv:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root=tk.Tk();root.withdraw()
            messagebox.showerror('上位机启动失败',str(exc)+'\n请查看 bench_app/runtime/startup_error.log')
            root.destroy()
        except Exception:
            pass
    if sys.stderr:sys.stderr.write(error)
    raise SystemExit(1)
