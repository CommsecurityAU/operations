import sqlite3
c = sqlite3.connect("data/ops.db")
rows = list(c.execute("""
    SELECT cl.task, cl.detail, cl.from_plan
    FROM claim_line cl JOIN project p ON p.id = cl.project_id
    WHERE p.name = '200 Victoria - IBP'"""))
for r in rows:
    print(r)
print(len(rows), "claims")
print("items:", list(c.execute("""
    SELECT i.name FROM claim_item i JOIN project p ON p.id = i.project_id
    WHERE p.name = '200 Victoria - IBP'""")))
