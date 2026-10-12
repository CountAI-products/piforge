"""Reusable sparse image extents, with checksums verified while copying."""
import errno
import hashlib
import json
import os
import re
import subprocess
import time

CHUNK = 8 * 1024 * 1024


def signature(path):
    info = os.stat(path)
    return [info.st_size, info.st_mtime_ns]


def extents(path):
    """Find allocated extents without an ext4 inode scan; holes need no writes."""
    size = os.path.getsize(path)
    with open(path, 'rb', buffering=0) as source:
        position = 0
        while position < size:
            try:
                start = os.lseek(source.fileno(), position, os.SEEK_DATA)
                end = min(os.lseek(source.fileno(), start, os.SEEK_HOLE), size)
            except OSError as error:
                if error.errno == errno.ENXIO:
                    break
                if error.errno in (errno.EINVAL, errno.ENOTSUP):
                    # On a non-sparse-capable filesystem fall back to a full map.
                    start, end = position, size
                else:
                    raise
            if end <= start:
                raise ValueError('Invalid sparse extent')
            while start < end:
                length = min(CHUNK, end-start)
                yield start, length
                start += length
            position = end


def required_extents(path, partitions):
    """Include allocated ext4 blocks even if their image bytes are zero holes.

    debugfs stats reads group descriptors, not the full inode tree. Bitmap reads
    happen only once when preparing the map. This matters on reused cards:
    skipping a zero-valued used block would leave the previous card's bytes.
    """
    intervals = [(offset,offset+length) for offset,length in extents(path)]
    if '?' in str(path):
        raise ValueError('Image paths containing ? are unsupported')
    with open(path,'rb',buffering=0) as source:
        for part in partitions:
            base, size = part['start']*512, part['size']*512
            if part.get('fstype') != 'ext4':
                intervals.append((base,base+size))
                continue
            result = subprocess.run(['debugfs','-R','stats',f'{path}?offset={base}'],capture_output=True,text=True,env={**os.environ,'LC_ALL':'C'})
            stats = result.stdout
            def value(label):
                match=re.search(r'^'+re.escape(label)+r':\s+(\d+)',stats,re.M)
                if not match:
                    raise ValueError('Cannot read ext4 block allocation metadata')
                return int(match.group(1))
            blocks, block_size = value('Block count'), value('Block size')
            first, per_group = value('First block'), value('Blocks per group')
            table_size=value('Inode blocks per group')
            if blocks*block_size > size:
                raise ValueError('Filesystem exceeds partition size')
            if 'bigalloc' in stats:
                raise ValueError('bigalloc ext4 is not supported by fast block mapping')
            groups=re.findall(r'Group\s+(\d+): block bitmap at (\d+), inode bitmap at (\d+), inode table at (\d+)',stats)
            expected=(blocks-first+per_group-1)//per_group
            if len(groups)!=expected:
                raise ValueError('Incomplete ext4 group descriptor list')
            for group,bitmap,inode_bitmap,inode_table in groups:
                group,bitmap,inode_bitmap,inode_table=map(int,(group,bitmap,inode_bitmap,inode_table))
                for block,length in ((bitmap,1),(inode_bitmap,1),(inode_table,table_size)):
                    intervals.append((base+block*block_size,base+(block+length)*block_size))
                source.seek(base+bitmap*block_size)
                bits=source.read(block_size)
                if len(bits)!=block_size:
                    raise ValueError('Truncated block bitmap')
                group_start=first+group*per_group
                count=min(per_group,blocks-group_start)
                run=None
                for index in range(count):
                    allocated=bool(bits[index//8] & (1<<(index%8)))
                    if allocated and run is None:run=index
                    if run is not None and (not allocated or index==count-1):
                        end=index if not allocated else index+1
                        intervals.append((base+(group_start+run)*block_size,base+(group_start+end)*block_size))
                        run=None
    merged=[]
    for start,end in sorted(intervals):
        if merged and start<=merged[-1][1]:merged[-1][1]=max(merged[-1][1],end)
        else:merged.append([start,end])
    for start,end in merged:
        while start<end:
            length=min(CHUNK,end-start)
            yield start,length
            start+=length


def build(path, progress=lambda done,total: None, partitions=None):
    before = signature(path)
    ranges = list(required_extents(path,partitions)) if partitions else list(extents(path))
    total = sum(length for _,length in ranges)
    done = 0
    records = []
    with open(path,'rb',buffering=0) as source:
        for offset,length in ranges:
            source.seek(offset)
            data = source.read(length)
            if len(data) != length:
                raise ValueError('Image truncated while creating block map')
            records.append([offset,length,hashlib.sha256(data).hexdigest()])
            done += length
            progress(done,total)
    if signature(path) != before:
        raise ValueError('Image changed while creating block map')
    return {'version':1,'image_bytes':before[0],'mapped_bytes':total,'ranges':records}


def validate(mapping, image_size):
    if mapping.get('version') != 1 or mapping.get('image_bytes') != image_size:
        raise ValueError('Block map version or image size mismatch')
    previous = total = 0
    for record in mapping.get('ranges',[]):
        if not isinstance(record,list) or len(record) != 3:
            raise ValueError('Malformed block-map range')
        offset,length,digest = record
        if type(offset) is not int or type(length) is not int or offset < previous or length <= 0 or length > CHUNK or offset+length > image_size:
            raise ValueError('Invalid or overlapping block-map ranges')
        if not isinstance(digest,str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('Invalid block-map checksum')
        previous = offset+length
        total += length
    if not total or total != mapping.get('mapped_bytes'):
        raise ValueError('Invalid block-map byte count')


def copy(source_path, target_path, mapping, progress=lambda done,total: None):
    validate(mapping,os.path.getsize(source_path))
    before = signature(source_path)
    done = 0
    with open(source_path,'rb',buffering=0) as source:
        descriptor = os.open(target_path,os.O_RDWR)
        try:
            for offset,length,digest in mapping['ranges']:
                source.seek(offset)
                data=source.read(length)
                if len(data) != length or hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError('Image checksum mismatch; re-create the block map')
                view=memoryview(data)
                written=0
                while written < length:
                    count=os.pwrite(descriptor,view[written:],offset+written)
                    if count <= 0:
                        raise OSError('Short target write')
                    written += count
                done += length
                progress(done,mapping['mapped_bytes'])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if signature(source_path) != before:
        raise ValueError('Image changed during copying')


def ensure(image_path, metadata_path, progress=lambda done,total: None):
    """One map build shared by concurrent burners, stored in the shareable JSON."""
    import fcntl
    with open(metadata_path+'.map-lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        with open(metadata_path) as file:
            metadata=json.load(file)
        mapping=metadata.get('block_map')
        if mapping:
            validate(mapping,os.path.getsize(image_path))
            return mapping
        mapping=build(image_path,progress,metadata.get("partitions"))
        validate(mapping,os.path.getsize(image_path))
        metadata['block_map']=mapping
        temporary=metadata_path+'.map-part'
        with open(temporary,'w') as file:
            json.dump(metadata,file,indent=2)
        os.replace(temporary,metadata_path)
        return mapping
