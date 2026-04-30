<?php

declare(strict_types=1);

namespace App\Example;

use Illuminate\Support\Facades\DB;

class ViolationExample
{
    public function badMethod(): array
    {
        $result = DB::table('users')->get();

        return compact('result');
    }
}
