import pathlib
p = pathlib.Path("_diag_template.py")
p.write_text("")  # clear
print("Template cleared")
