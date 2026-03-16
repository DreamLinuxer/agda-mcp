module Test where

open import Agda.Builtin.Nat

add : Nat → Nat → Nat
add zero    m = m
add (suc n) m = suc (add n m)

double : Nat → Nat
double n = {!   !}

id' : {A : Set} → A → A
id' x = {!   !}

myLemma : Nat → Nat → Nat
myLemma x y = {!   !}
