from __future__ import annotations
import os, ctypes, contextlib, pathlib, re, fcntl, functools, mmap, struct, tempfile, hashlib, subprocess, time, array
from typing import Tuple, List, Any, cast, Union, Dict
from dataclasses import dataclass
import tinygrad.runtime.autogen.libc as libc

@dataclass(frozen=True)
class ElfSection:
  name: str
  header: libc.Elf64_Shdr
  content: bytes

def elf_loader(blob:bytes, force_section_align:int=1) -> Tuple[memoryview, List[ElfSection], Any]:
  def _elf_parse_names(tabs): return {sum(len(w) + 1 for w in tabs.split(b'\0')[:i]): w.decode('utf-8')  for i, w in enumerate(tabs.split(b'\0'))}

  header = libc.Elf64_Ehdr.from_buffer_copy(blob)
  section_headers = (libc.Elf64_Shdr * header.e_shnum).from_buffer_copy(blob[header.e_shoff:])
  section_names = _elf_parse_names(blob[(shstrst:=section_headers[header.e_shstrndx].sh_offset):shstrst+section_headers[header.e_shstrndx].sh_size])
  sections = [ElfSection(section_names[sh.sh_name], sh, blob[sh.sh_offset:sh.sh_offset+sh.sh_size]) for sh in section_headers]

  def _to_carray(sh, ctype): return (ctype * (sh.header.sh_size // sh.header.sh_entsize)).from_buffer_copy(sh.content)
  rel = [(sh, sh.name[4:], _to_carray(sh, libc.Elf64_Rel)) for sh in sections if sh.header.sh_type == libc.SHT_REL]
  rela = [(sh, sh.name[5:], _to_carray(sh, libc.Elf64_Rela)) for sh in sections if sh.header.sh_type == libc.SHT_RELA]
  symtab = [_to_carray(sh, libc.Elf64_Sym) for sh in sections if sh.header.sh_type == libc.SHT_SYMTAB][0]
  progbits = [sh for sh in sections if sh.header.sh_type == libc.SHT_PROGBITS]

  # Prealloc image for all fixed addresses.
  image = bytearray(max([sh.header.sh_addr + sh.header.sh_size for sh in progbits if sh.header.sh_addr != 0] + [0]))
  for sh in progbits:
    if sh.header.sh_addr != 0: image[sh.header.sh_addr:sh.header.sh_addr+sh.header.sh_size] = sh.content
    else:
      image += b'\0' * (((align:=max(sh.header.sh_addralign, force_section_align)) - len(image) % align) % align) + sh.content
      sh.header.sh_addr = len(image) - len(sh.content)

  # Relocations
  relocs = []
  for sh, trgt_sh_name, rels in rel + rela:
    target_image_off = next(tsh for tsh in sections if tsh.name == trgt_sh_name).header.sh_addr

    for r in rels:
      rel_image_off = sections[(rel_sym:=symtab[libc.ELF64_R_SYM(r.r_info)]).st_shndx].header.sh_addr + rel_sym.st_value
      relocs.append((target_image_off + r.r_offset, rel_image_off, libc.ELF64_R_TYPE(r.r_info), getattr(r, "r_addend", 0)))

  return memoryview(image), sections, relocs
