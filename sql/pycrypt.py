#!/usr/bin/python
# -*- coding: utf-8 -*-

from Crypto.Cipher import AES
from binascii import b2a_hex, a2b_hex
import hashlib

class MyCrypt:
    """
    使用方法
    加密:
    crypter = MyCrypt(key)
    cipher_text = crypter.encrypt(text)

    解密:
    crypter = MyCrypt(key)
    plain_text = crypter.decrypt(text)
    """
    def __init__(self, key):
        self.key = key.encode("utf-8")

    def encrypt(self, text):
        key = hashlib.sha256(self.key).digest()
        cryptor = AES.new(key, AES.MODE_ECB)
        length = 16
        text_len = len(text)
        add = length - (text_len % length)
        text += "\0" * add
        cipher_text = b2a_hex(cryptor.encrypt(text))
        return cipher_text

    def decrypt(self, text):
        key = hashlib.sha256(self.key).digest()
        cryptor = AES.new(key, AES.MODE_ECB)
        plain_byte = cryptor.decrypt(a2b_hex(text))
        plain_text = plain_byte.decode(encoding="utf8").rstrip("\0")
        return plain_text

class ServerError(Exception):
    """
    self define exception
    自定义异常
    """
    def __init__(self,message):
        Exception.__init__(self)
        self.message=message