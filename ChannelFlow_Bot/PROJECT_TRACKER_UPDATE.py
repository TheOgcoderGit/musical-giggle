with open('PROJECT_TRACKER.md', 'r') as f:
    lines = f.readlines()
for i, line in enumerate(lines):
    if 'Current Phase:' in line:
        lines[i] = 'Current Phase:\nPhase 10 IN PROGRESS\n'
        break
with open('PROJECT_TRACKER.md', 'w') as f:
    f.writelines(lines)
print('DONE')