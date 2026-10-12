#!/usr/bin/env python3
"""Pack a PiForge image + block map for sharing, retaining sparse holes."""
import argparse
import os
from pathlib import Path
import subprocess
import blockmap


def export(image, output):
    image=Path(image).expanduser().resolve(strict=True)
    metadata=image.with_suffix('.json')
    output=Path(output).expanduser().absolute()
    if not image.is_file() or image.suffix != '.img' or not metadata.is_file():
        raise ValueError('Select a PiForge .img with matching .json metadata')
    if output.exists() or output in (image,metadata):
        raise ValueError('Choose a new archive filename')
    blockmap.ensure(str(image),str(metadata))
    temporary=str(output)+'.part'
    try:
        subprocess.run(['tar','--sparse','-czf',temporary,'-C',str(image.parent),image.name,metadata.name],check=True)
        os.replace(temporary,output)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image')
    parser.add_argument('archive',help='New .tar.gz output file')
    args=parser.parse_args()
    print(export(args.image,args.archive))

if __name__=='__main__':main()
