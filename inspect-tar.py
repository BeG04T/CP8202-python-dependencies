import tarfile, os, collections

TAR = "hard-gists.tar.gz"

ext_counts = collections.Counter()
sample_files = []
total = 0

with tarfile.open(TAR, 'r:gz') as tar:
    for m in tar.getmembers():
        if m.isfile():
            total += 1
            ext = os.path.splitext(m.name)[1] or '(no ext)'
            ext_counts[ext] += 1
            if len(sample_files) < 30:
                sample_files.append(m.name)

for ext, count in ext_counts.most_common():
    print(f"  {ext:20s} {count}")
for f in sample_files:
    print(f"  {f}")