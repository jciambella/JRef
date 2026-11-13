"""Walk through the phone interface on a PC and save a screenshot of every step.

A throw-away library is created and shared as "the desktop" (journal in a temporary folder); the mobile app is
then started on that folder like a phone would be, and driven through its screens: first run, list, folders,
search, a document, editing, adding a PDF, a change arriving from the desktop, settings. At the end the script
prints what each side sees. Needs kivy, PyMuPDF (to fabricate PDFs) and a display (or: xvfb-run -a python ...).

    python packaging/android/demo_walkthrough.py <output folder>
"""
import os, shutil, sys, tempfile
from pathlib import Path
OUT = Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
REPO = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(REPO))
work = Path(tempfile.mkdtemp(prefix="jrefdemo"))
root = work / "Dropbox" / "JRef"
os.environ["JREF_MOBILE_HOME"] = str(work / "phone-private")
os.environ["KIVY_NO_ARGS"] = "1"; os.environ["KIVY_METRICS_DENSITY"] = "2"

import pymupdf
from bibmanager.core import AppConfig, Author, Library, PaperMetadata

def pdf(name, title, lines=()):
    doc = pymupdf.open(); page = doc.new_page(); page.insert_textbox(pymupdf.Rect(72, 60, 540, 150), title, fontsize=18)
    y = 180
    for line in (*lines, name): page.insert_text((72, y), line, fontsize=10); y += 14
    p = work / name; doc.save(p); doc.close(); return p

def M(authors, year, title, journal="", **kw):
    return PaperMetadata(title=title, authors=[Author.from_full_name(a) for a in authors], year=year, journal=journal, **kw)

cfg = AppConfig.load(REPO / "config.json", library_root=root, import_mode="copy", sync_enabled=True, device_name="Office PC",
                     local_data_dir=work / "desk-private")
desk = Library(cfg)
thesis = desk.create_collection("PhD thesis Stefano"); visco = desk.create_collection("Viscoelastic fracture")
ch2 = desk.create_collection("Chapter 2", parent_id=thesis.id); tyre = desk.create_collection("Tyre talk 2027")
DATA = [
 (["Miehe, Christian", "Welschinger, Fabian", "Hofacker, Martina"], 2010, "Thermodynamically consistent phase-field models of fracture: Variational principles and multi-field FE implementations", "International Journal for Numerical Methods in Engineering", [thesis, ch2], True),
 (["Francfort, G. A.", "Marigo, J.-J."], 1998, "Revisiting brittle fracture as an energy minimization problem", "Journal of the Mechanics and Physics of Solids", [thesis, visco], True),
 (["Bourdin, Blaise", "Francfort, Gilles", "Marigo, Jean-Jacques"], 2000, "Numerical experiments in revisited brittle fracture", "Journal of the Mechanics and Physics of Solids", [thesis], False),
 (["Arruda, Ellen M.", "Boyce, Mary C."], 1993, "A three-dimensional constitutive model for the large stretch behavior of rubber elastic materials", "Journal of the Mechanics and Physics of Solids", [tyre], True),
 (["Bergström, J. S.", "Boyce, M. C."], 1998, "Constitutive modeling of the large strain time-dependent behavior of elastomers", "Journal of the Mechanics and Physics of Solids", [tyre, visco], True),
 (["Gent, A. N."], 1996, "A new constitutive relation for rubber", "Rubber Chemistry and Technology", [tyre], False),
 (["Reese, Stefanie", "Govindjee, Sanjay"], 1998, "A theory of finite viscoelasticity and numerical aspects", "International Journal of Solids and Structures", [visco, ch2], True),
 (["Simo, J. C."], 1987, "On a fully three-dimensional finite-strain viscoelastic damage model: Formulation and computational aspects", "Computer Methods in Applied Mechanics and Engineering", [visco], True),
 (["Ambrosio, Luigi", "Tortorelli, Vincenzo Maria"], 1990, "Approximation of functionals depending on jumps by elliptic functionals via Γ-convergence", "Communications on Pure and Applied Mathematics", [thesis], False),
 (["Holzapfel, Gerhard A.", "Gasser, Thomas C.", "Ogden, Ray W."], 2000, "A new constitutive framework for arterial wall mechanics and a comparative study of material models", "Journal of Elasticity", [], True),
 (["Anand, Lallit"], 2012, "A Cahn–Hilliard-type theory for species diffusion coupled with large elastic–plastic deformations", "Journal of the Mechanics and Physics of Solids", [ch2], True),
 (["Ciambella, Jacopo", "Nardinocchi, Paola"], 2021, "A structurally frame-indifferent model for anisotropic visco-hyperelastic materials", "Journal of the Mechanics and Physics of Solids", [visco, tyre], True),
]
ids = []
for i, (authors, year, title, journal, folders, has_pdf) in enumerate(DATA):
    meta = M(authors, year, title, journal, doi=f"10.5555/demo.{i}", volume=str(40 + i), pages=f"{100 + i * 7}-{130 + i * 7}",
             abstract="The variational approach regularises sharp crack discontinuities by an auxiliary scalar field. We outline a thermodynamically consistent framework for rate-independent diffusive fracture and discuss the numerical implementation with operator splits." if i < 3 else "")
    if has_pdf:
        p = desk.ingest_pdf(pdf(f"demo{i}.pdf", title), folders[0].id if folders else None, metadata=meta).paper
    else:
        p = desk.add_reference(meta, folders[0].id if folders else None).paper
    for f in folders[1:]: desk.add_to_collection(p.id, f.id)
    ids.append(p.id)
desk.add_supplement(ids[0], desk.prepare_attachment(pdf("si.pdf", "Supplementary information")), mode="copy", label="Supplementary Information")
desk.update_flags(ids[0], favorite=True, is_read=True, tags="phase-field; key reference", notes="Eq. (57): degradation function. Compare with the AT2 model used in Chapter 2.")
desk.update_flags(ids[1], favorite=True, is_read=True); desk.update_flags(ids[4], is_read=True, tags="tyre")
desk.update_flags(ids[11], my_publication=True, is_read=True, favorite=True)
for i in (3, 6, 7): desk.update_flags(ids[i], is_read=True)
# one PDF "not synchronised yet" on the phone
missing = desk.pdf_path(desk.get_paper(ids[7])); missing.rename(missing.with_suffix(".pdf.part"))
print("desktop pushed", desk.sync_now().pushed)

from kivy.config import Config
Config.set("graphics", "width", "720"); Config.set("graphics", "height", "1480"); Config.set("kivy", "exit_on_escape", "0")
from kivy.clock import Clock
from kivy.core.window import Window
from bibmanager.mobile import app as mobile, droid

app = mobile.JRefApp()
state = {"n": 0}
def shot(name):
    state["n"] += 1
    path = OUT / f"{state['n']:02d}_{name}.png"
    Window.screenshot(name=str(path))  # kivy appends a counter
    return path
steps = []
def step(delay, fn): steps.append((delay, fn))

access = {"ok": False}
droid.has_storage_access = lambda: access["ok"]
droid.request_storage_access = lambda: access.update(ok=True)
droid.download_dir = lambda: str(work)
opened = []
droid.open_file = lambda path, mime="application/pdf": opened.append(str(path)) or True

def s_setup(): shot("setup")
def s_grant():
    app.request_access(); app.setup_folder_chosen(str(root)); app.refresh_setup(str(root))
def s_setup2(): shot("setup_ready")
def s_browse(): app.browse("folder", str(root.parent), app.setup_folder_chosen)
def s_browse2(): shot("folder_browser"); app.go_back()
def s_open(): app.finish_setup()
def s_list(): shot("library")
def s_drawer(): app.open_drawer()
def s_drawer2(): shot("drawer")
def close_modals():
    from kivy.uix.modalview import ModalView
    for w in list(Window.children):
        if isinstance(w, ModalView): w.dismiss(animation=False)
def s_folder():
    close_modals(); lib = app.library
    app.select_node(("folder", next(c.id for c in lib.collections() if c.name == "Viscoelastic fracture")))
def s_folder2(): shot("folder_view")
def s_search():
    app.select_node(("all", None)); app.screen("library").ids.search.text = "author:boyce year:1990-1999"
def s_search2(): shot("search")
def s_paper():
    app.screen("library").ids.search.text = ""
    app.show_paper(app.library.db.get_paper_by_citation_key("Miehe_et_al_2010").id)
def s_paper2(): shot("paper")
def s_menu(): app.paper_menu()
def s_menu2(): shot("paper_menu"); close_modals()
def s_folders(): app.edit_folders()
def s_folders2(): shot("paper_folders"); close_modals()
def s_notes(): app.edit_notes()
def s_notes2(): shot("notes_dialog"); close_modals()
def s_openpdf():
    app.open_pdf(); print("opened", opened)
def s_edit(): app.edit_paper()
def s_edit2():
    shot("edit")
    app._edit_inputs["authors"].text = "Miehe, Christian; Welschinger, Fabian"; app._edit_inputs["year"].text = "2011"
    app.save_edit()
def s_edit3(): shot("paper_renamed")
def s_add(): app.go_back(); app.add_menu()
def s_add2(): shot("add_menu"); close_modals()
def s_import():
    app.add_to_folder = None
    app.import_pdf(str(pdf("download.pdf", "Fracture of soft elastic foam", ["doi: 10.5555/none.found"])))
def s_import2(): shot("after_import")
def s_missing():
    app.show_paper(app.library.db.get_paper_by_citation_key("Simo_1987").id)
def s_missing2(): shot("pdf_not_synced")
def s_desktop_change():
    # meanwhile, on the PC ...
    desk.sync_now()
    p = desk.ingest_pdf(pdf("pc.pdf", "Added on the PC"), tyre.id, metadata=M(["Treloar, L. R. G."], 1944, "Stress-strain data for vulcanised rubber under various types of deformation", "Transactions of the Faraday Society")).paper
    desk.update_flags(desk.db.get_paper_by_citation_key("Gent_1996").id, favorite=True)
    print("desktop sees phone papers:", sorted(x.citation_key for x in desk.papers())[:20]); print("desktop pushed", desk.sync_now().pushed)
    app.go_back(); app.sync_now(manual=True)
def s_synced(): shot("synced")
def s_settings(): app.open_settings()
def s_settings2(): shot("settings")
def s_quit():
    desk.sync_now()
    print("FINAL desktop:", sorted(x.citation_key for x in desk.papers()))
    print("FINAL phone  :", sorted(x.citation_key for x in app.library.papers()))
    print("journals:", sorted(str(p.relative_to(root)) for p in (root / "sync").rglob("*.jsonl")))
    print("db in folder:", (root / "library.db").exists(), "| bib files:", sorted(p.name for p in (root / "bibtex").glob("*.bib")))
    desk.close(); app.stop()

for d, f in [(1.2, s_setup), (0.3, s_grant), (0.5, s_setup2), (0.2, s_browse), (0.5, s_browse2), (0.3, s_open), (1.2, s_list), (0.2, s_drawer), (0.8, s_drawer2),
             (0.2, s_folder), (0.6, s_folder2), (0.2, s_search), (0.8, s_search2), (0.2, s_paper), (0.7, s_paper2), (0.2, s_menu), (0.7, s_menu2),
             (0.3, s_folders), (0.7, s_folders2), (0.3, s_notes), (0.8, s_notes2), (0.3, s_openpdf), (0.3, s_edit), (0.7, s_edit2), (0.7, s_edit3),
             (0.3, s_add), (0.7, s_add2), (0.3, s_import), (2.0, s_import2), (0.3, s_missing), (0.7, s_missing2), (0.3, s_desktop_change), (1.0, s_synced),
             (0.3, s_settings), (0.8, s_settings2), (0.5, s_quit)]:
    step(d, f)
def run_next(_dt=0):
    if not steps: return
    delay, fn = steps.pop(0)
    def go(_dt):
        try: fn()
        except Exception:
            import traceback; traceback.print_exc(); app.stop(); return
        run_next()
    Clock.schedule_once(go, delay)
app.bind(on_start=lambda *_a: run_next())
app.run()
